"""Interactive, privacy-safe acceptance test for the real voice path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from companion.core.event_bus import EventBus
from companion.events.base import BaseEvent
from companion.events.conversation import ConversationTurnFailedEvent


class VoiceAcceptanceMicrophone(Protocol):
    @property
    def speech_start_sequence(self) -> int: ...

    async def get_speech_audio(self, timeout: float = 15.0) -> bytes | None: ...

    async def wait_for_speech_start(
        self, after_sequence: int, timeout: float
    ) -> int | None: ...


class VoiceAcceptancePipeline(Protocol):
    async def process_audio_input(self, audio_bytes: bytes) -> str: ...

    async def interrupt(self) -> bool: ...

    def get_current_state(self) -> Any: ...


@dataclass(frozen=True)
class VoiceAcceptanceCheck:
    code: str
    passed: bool
    message: str
    actual_ms: int | None = None
    target_ms: int | None = None


@dataclass(frozen=True)
class VoiceAcceptanceReport:
    checks: list[VoiceAcceptanceCheck]

    @property
    def exit_code(self) -> int:
        return 0 if self.checks and all(check.passed for check in self.checks) else 1

    def to_json(self) -> str:
        return json.dumps(
            {
                "exit_code": self.exit_code,
                "passed": self.exit_code == 0,
                "checks": [asdict(check) for check in self.checks],
            },
            ensure_ascii=False,
            indent=2,
        )


def failed_voice_acceptance_report(code: str, message: str) -> VoiceAcceptanceReport:
    """Build a stable failure result for setup paths outside the interactive runner."""
    return VoiceAcceptanceReport([VoiceAcceptanceCheck(code, False, message)])


_VOICE_EVENT_TYPES = (
    "conversation.turn.started",
    "conversation.asr.finalized",
    "conversation.llm.response",
    "conversation.tts.synthesized",
    "conversation.audio.played",
    "conversation.turn.completed",
    "conversation.turn.interrupted",
    "conversation.turn.failed",
)


async def run_voice_acceptance(
    *,
    microphone: VoiceAcceptanceMicrophone,
    pipeline: VoiceAcceptancePipeline,
    event_bus: EventBus,
    sample_rate: int,
    utterance_timeout_seconds: float,
    turn_timeout_seconds: float,
    target_e2e_latency_ms: int,
    target_interrupt_latency_ms: int,
    barge_in_guard_seconds: float = 0.5,
    announce: Callable[[str], None] | None = None,
) -> VoiceAcceptanceReport:
    """Exercise one complete spoken turn and one real-microphone interruption."""
    if min(sample_rate, utterance_timeout_seconds, turn_timeout_seconds) <= 0:
        raise ValueError("voice acceptance timeouts and sample rate must be positive")
    if barge_in_guard_seconds < 0:
        raise ValueError("voice acceptance barge-in guard must not be negative")
    output = announce or (lambda _message: None)
    events: list[BaseEvent] = []

    def capture(event: BaseEvent) -> None:
        events.append(event)

    for event_type in _VOICE_EVENT_TYPES:
        event_bus.subscribe(event_type, capture)
    try:
        output("Speak a short sentence for the complete spoken-turn test.")
        first_audio = await microphone.get_speech_audio(timeout=utterance_timeout_seconds)
        if not _usable_utterance(first_audio, sample_rate):
            return VoiceAcceptanceReport(
                [
                    VoiceAcceptanceCheck(
                        "voice.complete_input",
                        False,
                        "No usable microphone utterance was captured before the deadline.",
                    )
                ]
            )
        assert first_audio is not None

        first_start = len(events)
        try:
            await _run_turn(
                pipeline.process_audio_input(first_audio), timeout_seconds=turn_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return failed_voice_acceptance_report(
                "voice.complete_turn",
                f"The complete spoken turn failed: {type(exc).__name__}.",
            )
        first_events = events[first_start:]
        checks = _complete_turn_checks(
            first_events, target_e2e_latency_ms=target_e2e_latency_ms
        )
        if not all(check.passed for check in checks):
            return VoiceAcceptanceReport(checks)

        output("Speak another sentence to start the interruption test.")
        second_audio = await microphone.get_speech_audio(timeout=utterance_timeout_seconds)
        if not _usable_utterance(second_audio, sample_rate):
            checks.append(
                VoiceAcceptanceCheck(
                    "voice.interrupt_input",
                    False,
                    "No usable microphone utterance was captured for the interruption test.",
                )
            )
            return VoiceAcceptanceReport(checks)
        assert second_audio is not None

        second_start = len(events)
        response_task = asyncio.create_task(pipeline.process_audio_input(second_audio))
        barge_in_task: asyncio.Task[bytes | None] | None = None
        try:
            speaking = await _wait_until_speaking(
                pipeline, response_task, timeout_seconds=turn_timeout_seconds
            )
            if not speaking:
                await _settle_task(response_task)
                checks.append(_interruption_setup_failure(events[second_start:]))
                return VoiceAcceptanceReport(checks)

            speech_start_sequence = microphone.speech_start_sequence
            barge_in_task = asyncio.create_task(
                microphone.get_speech_audio(timeout=utterance_timeout_seconds)
            )
            if barge_in_guard_seconds:
                premature_sequence = await microphone.wait_for_speech_start(
                    speech_start_sequence, timeout=barge_in_guard_seconds
                )
                if premature_sequence is not None:
                    await _settle_task(barge_in_task)
                    checks.append(
                        VoiceAcceptanceCheck(
                            "voice.echo_guard",
                            False,
                            "VAD fired before the barge-in prompt; "
                            "check speaker echo or crosstalk.",
                        )
                    )
                    return VoiceAcceptanceReport(checks)
                if response_task.done():
                    await _settle_task(barge_in_task)
                    checks.append(_interruption_setup_failure(events[second_start:]))
                    return VoiceAcceptanceReport(checks)

            output("The companion is speaking. Speak now to test barge-in.")
            detected_sequence = await microphone.wait_for_speech_start(
                speech_start_sequence, timeout=utterance_timeout_seconds
            )
            if detected_sequence is None:
                await _settle_task(barge_in_task)
                checks.append(
                    VoiceAcceptanceCheck(
                        "voice.barge_in_input",
                        False,
                        "No VAD speech-start signal was detected while playback was active.",
                    )
                )
                return VoiceAcceptanceReport(checks)

            started = time.perf_counter()
            try:
                interrupted = await pipeline.interrupt()
                interrupt_ms = int((time.perf_counter() - started) * 1000)
                await _run_turn(response_task, timeout_seconds=turn_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                checks.append(
                    VoiceAcceptanceCheck(
                        "voice.interrupt_terminal",
                        False,
                        f"Barge-in failed: {type(exc).__name__}.",
                    )
                )
                return VoiceAcceptanceReport(checks)
            barge_in_audio = await _run_turn(
                barge_in_task, timeout_seconds=utterance_timeout_seconds
            )
            if not _usable_utterance(barge_in_audio, sample_rate):
                checks.append(
                    VoiceAcceptanceCheck(
                        "voice.barge_in_input",
                        False,
                        "The VAD signal did not produce a usable barge-in utterance.",
                    )
                )
                return VoiceAcceptanceReport(checks)
            second_events = events[second_start:]
            interrupted_events = _events_for_turn(
                second_events, "conversation.turn.interrupted"
            )
            terminal_events = _terminal_events(second_events)
            interrupt_ok = (
                interrupted
                and len(interrupted_events) == 1
                and len(terminal_events) == 1
                and terminal_events[0].event_type == "conversation.turn.interrupted"
            )
            checks.append(
                VoiceAcceptanceCheck(
                    "voice.interrupt_terminal",
                    interrupt_ok,
                    "Barge-in produced one durable interrupted terminal event."
                    if interrupt_ok
                    else _failure_message(
                        second_events,
                        "Barge-in did not produce one durable interrupted terminal event.",
                    ),
                )
            )
            latency_ok = interrupt_ok and interrupt_ms <= target_interrupt_latency_ms
            checks.append(
                VoiceAcceptanceCheck(
                    "voice.interrupt_latency",
                    latency_ok,
                    "Provider cancellation and playback stop met the interruption target."
                    if latency_ok
                    else "Provider cancellation or playback stop exceeded the interruption target.",
                    actual_ms=interrupt_ms,
                    target_ms=target_interrupt_latency_ms,
                )
            )
            return VoiceAcceptanceReport(checks)
        finally:
            _cancel_and_detach(response_task)
            if barge_in_task is not None:
                _cancel_and_detach(barge_in_task)
    finally:
        for event_type in _VOICE_EVENT_TYPES:
            event_bus.unsubscribe(event_type, capture)


def render_voice_acceptance_report(report: VoiceAcceptanceReport) -> str:
    lines = ["Voice acceptance report"]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        timing = ""
        if check.actual_ms is not None and check.target_ms is not None:
            timing = f" ({check.actual_ms}ms / target {check.target_ms}ms)"
        lines.append(f"[{status}] {check.code}: {check.message}{timing}")
    lines.append("Result: PASS" if report.exit_code == 0 else "Result: FAIL")
    return "\n".join(lines)


async def _run_turn(operation: Any, *, timeout_seconds: float) -> Any:
    task = operation if isinstance(operation, asyncio.Task) else asyncio.create_task(operation)
    done, _ = await asyncio.wait([task], timeout=timeout_seconds)
    if not done:
        _cancel_and_detach(task)
        raise TimeoutError("voice acceptance turn exceeded its deadline")
    return await task


async def _settle_task(task: asyncio.Task[Any]) -> None:
    _cancel_and_detach(task)
    await asyncio.sleep(0)


def _cancel_and_detach(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


async def _wait_until_speaking(
    pipeline: VoiceAcceptancePipeline,
    response_task: asyncio.Task[Any],
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if str(pipeline.get_current_state()) == "speaking":
            return True
        if response_task.done():
            return False
        await asyncio.sleep(0.01)
    return False


def _usable_utterance(audio: bytes | None, sample_rate: int) -> bool:
    return audio is not None and len(audio) >= sample_rate * 2 // 10


def _complete_turn_checks(
    events: list[BaseEvent], *, target_e2e_latency_ms: int
) -> list[VoiceAcceptanceCheck]:
    started = [event for event in events if event.event_type == "conversation.turn.started"]
    if len(started) != 1:
        return [
            VoiceAcceptanceCheck(
                "voice.complete_turn",
                False,
                _failure_message(events, "The spoken turn did not start exactly once."),
            )
        ]
    turn_id = str(getattr(started[0], "turn_id", ""))
    required = (
        "conversation.asr.finalized",
        "conversation.llm.response",
        "conversation.tts.synthesized",
        "conversation.audio.played",
        "conversation.turn.completed",
    )
    required_present = all(_events_for_turn(events, event_type, turn_id) for event_type in required)
    terminals = [
        event
        for event in _terminal_events(events)
        if str(getattr(event, "turn_id", "")) == turn_id
    ]
    complete_ok = (
        required_present
        and len(terminals) == 1
        and terminals[0].event_type == "conversation.turn.completed"
    )
    checks = [
        VoiceAcceptanceCheck(
            "voice.complete_turn",
            complete_ok,
            "Microphone, ASR, LLM, streaming TTS, playback, and durable completion all ran."
            if complete_ok
            else _failure_message(events, "The complete spoken-turn chain did not pass."),
        )
    ]
    completed = terminals[0] if complete_ok else None
    e2e_ms = int(getattr(completed, "total_latency_ms", 0)) if completed else 0
    latency_ok = complete_ok and 0 < e2e_ms <= target_e2e_latency_ms
    checks.append(
        VoiceAcceptanceCheck(
            "voice.first_audio_latency",
            latency_ok,
            "Voice-turn-to-first-audio latency met the configured target."
            if latency_ok
            else "Voice-turn-to-first-audio latency was missing or exceeded the configured target.",
            actual_ms=e2e_ms,
            target_ms=target_e2e_latency_ms,
        )
    )
    return checks


def _events_for_turn(
    events: list[BaseEvent], event_type: str, turn_id: str | None = None
) -> list[BaseEvent]:
    return [
        event
        for event in events
        if event.event_type == event_type
        and (turn_id is None or str(getattr(event, "turn_id", "")) == turn_id)
    ]


def _terminal_events(events: list[BaseEvent]) -> list[BaseEvent]:
    return [
        event
        for event in events
        if event.event_type
        in {
            "conversation.turn.completed",
            "conversation.turn.interrupted",
            "conversation.turn.failed",
        }
    ]


def _failure_message(events: list[BaseEvent], fallback: str) -> str:
    failures = [event for event in events if isinstance(event, ConversationTurnFailedEvent)]
    if not failures:
        return fallback
    failure = failures[-1]
    return f"Voice stage failed: {failure.stage}/{failure.error_type}."


def _interruption_setup_failure(events: list[BaseEvent]) -> VoiceAcceptanceCheck:
    return VoiceAcceptanceCheck(
        "voice.interrupt_setup",
        False,
        _failure_message(
            events,
            "The second turn ended before active playback could be interrupted.",
        ),
    )

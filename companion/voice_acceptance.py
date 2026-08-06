"""Interactive, privacy-safe acceptance test for the real voice path."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

from companion.acceptance_report import AcceptanceCheck, AcceptanceReport, failed_report
from companion.async_util import consume_task_result, wait_with_timeout
from companion.core.event_bus import EventBus
from companion.events.base import BaseEvent
from companion.events.conversation import (
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
)


class VoiceAcceptanceMicrophone(Protocol):
    @property
    def speech_start_sequence(self) -> int: ...

    async def get_speech_audio(self, timeout: float = 15.0) -> bytes | None: ...

    async def wait_for_speech_start(
        self, after_sequence: int, timeout: float
    ) -> int | None: ...


class VoiceAcceptanceSnapshot(Protocol):
    @property
    def terminal_state(self) -> str: ...

    @property
    def incremental_playback(self) -> bool | None: ...

    @property
    def pcm_continuous(self) -> bool: ...

    @property
    def played_segment_count(self) -> int: ...

    @property
    def output_underflow_count(self) -> int: ...

    @property
    def history_matches_played_text(self) -> bool: ...


class VoiceAcceptancePipeline(Protocol):
    async def process_audio_input(self, audio_bytes: bytes) -> str: ...

    async def interrupt(self) -> bool: ...

    def get_current_state(self) -> Any: ...

    def get_voice_acceptance_snapshot(self) -> VoiceAcceptanceSnapshot: ...


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

_MAX_NO_SPEECH_ATTEMPTS = 3
_COMPLETE_TURN_REQUEST = (
    "请先只说一句好的并用句号结束，然后不换行，写一个至少三百字的故事，"
    "中间只用逗号，最后才用句号，不要解释。"
)
_INTERRUPTION_REQUEST = (
    "请先只说一句好的并用句号结束，然后不换行，写一个至少六百字的故事，"
    "中间只用逗号，最后才用句号，不要解释。"
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
) -> AcceptanceReport:
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
        output(
            "【第一轮：完整播放】现在请对着麦克风清晰说出下面这句话，说完后保持安静，"
            f"等待 AD学姐完整播放结束：\n{_COMPLETE_TURN_REQUEST}"
        )
        complete_deadline = time.monotonic() + (
            utterance_timeout_seconds + turn_timeout_seconds
        )
        first_events: list[BaseEvent] = []
        for attempt in range(1, _MAX_NO_SPEECH_ATTEMPTS + 1):
            capture_timeout = _remaining_timeout(
                complete_deadline, utterance_timeout_seconds
            )
            first_audio = await microphone.get_speech_audio(timeout=capture_timeout)
            if not _usable_utterance(first_audio, sample_rate):
                return AcceptanceReport(
                    [
                        AcceptanceCheck(
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
                    pipeline.process_audio_input(first_audio),
                    timeout_seconds=_remaining_timeout(
                        complete_deadline, turn_timeout_seconds
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return failed_report(
                    "voice.complete_turn",
                    f"The complete spoken turn failed: {type(exc).__name__}.",
                )
            first_events = events[first_start:]
            if not _is_no_speech_failure(first_events):
                break
            if (
                attempt == _MAX_NO_SPEECH_ATTEMPTS
                or time.monotonic() >= complete_deadline
            ):
                break
            output("没有识别到语音，请立即把上面的完整句子再说一次。")

        checks = _complete_turn_checks(
            first_events,
            snapshot=pipeline.get_voice_acceptance_snapshot(),
            target_e2e_latency_ms=target_e2e_latency_ms,
        )
        if not all(check.passed for check in checks):
            return AcceptanceReport(checks)

        output(
            "【第二轮：打断】现在请对着麦克风清晰说出下面这句话，说完后保持安静，"
            f"等待 AD学姐开始播放：\n{_INTERRUPTION_REQUEST}"
        )
        interruption_deadline = time.monotonic() + (
            utterance_timeout_seconds + turn_timeout_seconds
        )
        second_start = len(events)
        response_task: asyncio.Task[str] | None = None
        barge_in_task: asyncio.Task[bytes | None] | None = None
        try:
            for attempt in range(1, _MAX_NO_SPEECH_ATTEMPTS + 1):
                capture_timeout = _remaining_timeout(
                    interruption_deadline, utterance_timeout_seconds
                )
                second_audio = await microphone.get_speech_audio(timeout=capture_timeout)
                if not _usable_utterance(second_audio, sample_rate):
                    checks.append(
                        AcceptanceCheck(
                            "voice.interrupt_input",
                            False,
                            "No usable microphone utterance was captured "
                            "for the interruption test.",
                        )
                    )
                    return AcceptanceReport(checks)
                assert second_audio is not None

                second_start = len(events)
                response_task = asyncio.create_task(
                    pipeline.process_audio_input(second_audio)
                )
                speaking = await _wait_until_speaking(
                    pipeline,
                    response_task,
                    timeout_seconds=_remaining_timeout(
                        interruption_deadline, turn_timeout_seconds
                    ),
                )
                if speaking:
                    break

                await _settle_task(response_task)
                second_events = events[second_start:]
                response_task = None
                if not _is_no_speech_failure(second_events):
                    checks.append(_interruption_setup_failure(second_events))
                    return AcceptanceReport(checks)
                if (
                    attempt == _MAX_NO_SPEECH_ATTEMPTS
                    or time.monotonic() >= interruption_deadline
                ):
                    checks.append(_interruption_setup_failure(second_events))
                    return AcceptanceReport(checks)
                output(
                    "没有识别到语音，请立即把第二轮的完整句子再说一次。"
                )

            if response_task is None:
                checks.append(_interruption_setup_failure(events[second_start:]))
                return AcceptanceReport(checks)

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
                        AcceptanceCheck(
                            "voice.echo_guard",
                            False,
                            "VAD fired before the barge-in prompt; "
                            "check speaker echo or crosstalk.",
                        )
                    )
                    return AcceptanceReport(checks)
                if response_task.done():
                    await _settle_task(barge_in_task)
                    checks.append(_interruption_setup_failure(events[second_start:]))
                    return AcceptanceReport(checks)

            output("【现在打断】AD学姐已经开始说话，请立刻持续说话 2 至 3 秒。")
            detected_sequence = await microphone.wait_for_speech_start(
                speech_start_sequence, timeout=utterance_timeout_seconds
            )
            if detected_sequence is None:
                await _settle_task(barge_in_task)
                checks.append(
                    AcceptanceCheck(
                        "voice.barge_in_input",
                        False,
                        "No VAD speech-start signal was detected while playback was active.",
                    )
                )
                return AcceptanceReport(checks)

            started = time.perf_counter()
            try:
                interrupted = await pipeline.interrupt()
                interrupt_ms = int((time.perf_counter() - started) * 1000)
                await _run_turn(response_task, timeout_seconds=turn_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                checks.append(
                    AcceptanceCheck(
                        "voice.interrupt_terminal",
                        False,
                        f"Barge-in failed: {type(exc).__name__}.",
                    )
                )
                return AcceptanceReport(checks)
            barge_in_audio = await _run_turn(
                barge_in_task, timeout_seconds=utterance_timeout_seconds
            )
            if not _usable_utterance(barge_in_audio, sample_rate):
                checks.append(
                    AcceptanceCheck(
                        "voice.barge_in_input",
                        False,
                        "The VAD signal did not produce a usable barge-in utterance.",
                    )
                )
                return AcceptanceReport(checks)
            second_events = events[second_start:]
            interrupted_events = _events_for_turn(
                second_events, "conversation.turn.interrupted"
            )
            terminal_events = _terminal_events(second_events)
            interrupt_ok = (
                interrupted
                and len(interrupted_events) == 1
                and len(terminal_events) == 1
                and isinstance(terminal_events[0], ConversationTurnCompletedEvent)
                and terminal_events[0].was_interrupted
            )
            checks.append(
                AcceptanceCheck(
                    "voice.interrupt_terminal",
                    interrupt_ok,
                    "Barge-in produced one interruption notice and one completed terminal."
                    if interrupt_ok
                    else _failure_message(
                        second_events,
                        "Barge-in did not produce one interruption notice and one terminal.",
                    ),
                )
            )
            latency_ok = interrupt_ok and interrupt_ms <= target_interrupt_latency_ms
            checks.append(
                AcceptanceCheck(
                    "voice.interrupt_latency",
                    latency_ok,
                    "Provider cancellation and playback stop met the interruption target."
                    if latency_ok
                    else "Provider cancellation or playback stop exceeded the interruption target.",
                    actual_ms=interrupt_ms,
                    target_ms=target_interrupt_latency_ms,
                )
            )
            interrupted_snapshot = pipeline.get_voice_acceptance_snapshot()
            interrupted_history_ok = (
                interrupt_ok
                and interrupted_snapshot.terminal_state == "completed"
                and interrupted_snapshot.history_matches_played_text
            )
            checks.append(
                AcceptanceCheck(
                    "voice.interrupted_history",
                    interrupted_history_ok,
                    "Interrupted history contains only text confirmed as played."
                    if interrupted_history_ok
                    else "Interrupted history did not match the confirmed played text.",
                )
            )
            return AcceptanceReport(checks)
        finally:
            if response_task is not None:
                _cancel_and_detach(response_task)
            if barge_in_task is not None:
                _cancel_and_detach(barge_in_task)
    finally:
        for event_type in _VOICE_EVENT_TYPES:
            event_bus.unsubscribe(event_type, capture)


async def _run_turn(operation: Any, *, timeout_seconds: float) -> Any:
    task = operation if isinstance(operation, asyncio.Task) else asyncio.create_task(operation)
    if not await wait_with_timeout(task, timeout_seconds):
        raise TimeoutError("voice acceptance turn exceeded its deadline")
    return await task


async def _settle_task(task: asyncio.Task[Any]) -> None:
    _cancel_and_detach(task)
    await asyncio.sleep(0)


def _cancel_and_detach(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(consume_task_result)


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


def _remaining_timeout(deadline: float, maximum_seconds: float) -> float:
    return max(0.0, min(maximum_seconds, deadline - time.monotonic()))


def _is_no_speech_failure(events: list[BaseEvent]) -> bool:
    terminals = _terminal_events(events)
    return (
        len(terminals) == 1
        and isinstance(terminals[0], ConversationTurnFailedEvent)
        and terminals[0].stage == "asr"
        and terminals[0].error_type == "no_speech_recognized"
    )


def _complete_turn_checks(
    events: list[BaseEvent],
    *,
    snapshot: VoiceAcceptanceSnapshot,
    target_e2e_latency_ms: int,
) -> list[AcceptanceCheck]:
    started = [event for event in events if event.event_type == "conversation.turn.started"]
    if len(started) != 1:
        return [
            AcceptanceCheck(
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
        AcceptanceCheck(
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
        AcceptanceCheck(
            "voice.first_audio_latency",
            latency_ok,
            "Voice-turn-to-first-audio latency met the configured target."
            if latency_ok
            else "Voice-turn-to-first-audio latency was missing or exceeded the configured target.",
            actual_ms=e2e_ms,
            target_ms=target_e2e_latency_ms,
        )
    )
    incremental_ok = (
        complete_ok
        and snapshot.terminal_state == "completed"
        and snapshot.incremental_playback is True
    )
    checks.append(
        AcceptanceCheck(
            "voice.incremental_playback",
            incremental_ok,
            "The first played audio began before the LLM stream completed."
            if incremental_ok
            else (
                "The first played audio did not begin before the LLM stream completed; "
                "repeat the gate using the supplied multi-sentence request."
            ),
        )
    )
    continuity_ok = (
        complete_ok
        and snapshot.pcm_continuous
        and snapshot.played_segment_count > 0
        and snapshot.output_underflow_count == 0
    )
    checks.append(
        AcceptanceCheck(
            "voice.pcm_continuity",
            continuity_ok,
            "All PCM chunks used one output stream without underflow."
            if continuity_ok
            else "PCM playback changed streams, underflowed, or produced no measured chunks.",
        )
    )
    completed_history_ok = (
        complete_ok
        and snapshot.terminal_state == "completed"
        and snapshot.history_matches_played_text
    )
    checks.append(
        AcceptanceCheck(
            "voice.completed_history",
            completed_history_ok,
            "Completed history exactly matches text confirmed as played."
            if completed_history_ok
            else "Completed history did not match the confirmed played text.",
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
            "conversation.turn.failed",
        }
    ]


def _failure_message(events: list[BaseEvent], fallback: str) -> str:
    failures = [event for event in events if isinstance(event, ConversationTurnFailedEvent)]
    if not failures:
        return fallback
    failure = failures[-1]
    return f"Voice stage failed: {failure.stage}/{failure.error_type}."


def _interruption_setup_failure(events: list[BaseEvent]) -> AcceptanceCheck:
    return AcceptanceCheck(
        "voice.interrupt_setup",
        False,
        _failure_message(
            events,
            "The second turn ended before active playback could be interrupted.",
        ),
    )

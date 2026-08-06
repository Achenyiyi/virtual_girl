"""Desktop microphone lifecycle without terminal output."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from companion.async_util import consume_task_result
from companion.audio.microphone import MicConfig, MicrophoneCapture
from companion.protocols.turn import TurnState

type VoiceStateCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DesktopVoiceController:
    """Own continuous VAD capture while allowing in-flight replies to finish."""

    def __init__(
        self,
        config: MicConfig,
        pipeline: Any,
        on_state_changed: VoiceStateCallback,
        *,
        microphone_factory: Callable[[MicConfig], MicrophoneCapture] = MicrophoneCapture,
    ) -> None:
        self._config = config
        self._pipeline = pipeline
        self._on_state_changed = on_state_changed
        self._microphone_factory = microphone_factory
        self._microphone: MicrophoneCapture | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._response_task: asyncio.Task[str] | None = None
        self._enabled = False
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> bool:
        async with self._lock:
            if self._enabled:
                return True
            microphone = self._microphone_factory(self._config)
            if not await microphone.start():
                await self._on_state_changed(
                    {"enabled": False, "state": "error", "error": "microphone_unavailable"}
                )
                return False
            self._microphone = microphone
            self._enabled = True
            self._loop_task = asyncio.create_task(self._run(microphone))
        await self._on_state_changed({"enabled": True, "state": "listening"})
        return True

    async def stop(self) -> None:
        async with self._lock:
            self._enabled = False
            loop_task, self._loop_task = self._loop_task, None
            microphone, self._microphone = self._microphone, None
            if loop_task is not None and not loop_task.done():
                loop_task.cancel()
            if microphone is not None:
                await microphone.stop()
            response_task = self._response_task
            if response_task is not None and not response_task.done():
                active_turn_state = getattr(self._pipeline, "active_turn_state", None)
                if active_turn_state in {
                    TurnState.STARTED,
                    TurnState.ASR_PROCESSING,
                }:
                    response_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await response_task
            if loop_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
        await self._on_state_changed({"enabled": False, "state": "off"})

    async def _run(self, microphone: MicrophoneCapture) -> None:
        speech_task: asyncio.Task[bytes | None] | None = asyncio.create_task(
            microphone.get_speech_audio(timeout=30.0)
        )
        speech_start_task: asyncio.Task[int | None] | None = None
        response_task: asyncio.Task[str] | None = None
        speech_sequence = microphone.speech_start_sequence
        try:
            while self._enabled:
                if response_task is None:
                    if speech_task is None:
                        break
                    audio = await speech_task
                    speech_task = asyncio.create_task(
                        microphone.get_speech_audio(timeout=30.0)
                    )
                    if not audio or len(audio) < 1600:
                        continue
                    await self._on_state_changed({"enabled": True, "state": "processing"})
                    response_task = asyncio.create_task(
                        self._pipeline.process_audio_input(audio)
                    )
                    self._response_task = response_task
                    speech_sequence = microphone.speech_start_sequence
                    speech_start_task = asyncio.create_task(
                        microphone.wait_for_speech_start(speech_sequence, timeout=30.0)
                    )

                waiters = {task for task in (response_task, speech_start_task) if task}
                if not waiters:
                    continue
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if response_task in done:
                    consume_task_result(response_task)
                    response_task = None
                    self._response_task = None
                    if speech_start_task is not None:
                        speech_start_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await speech_start_task
                        speech_start_task = None
                    await self._on_state_changed({"enabled": True, "state": "listening"})
                    continue
                if speech_start_task in done:
                    detected = speech_start_task.result()
                    if detected is not None:
                        speech_sequence = detected
                        await self._pipeline.interrupt()
                    speech_start_task = asyncio.create_task(
                        microphone.wait_for_speech_start(speech_sequence, timeout=30.0)
                    )
        except asyncio.CancelledError:
            raise
        finally:
            for task in (speech_task, speech_start_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if response_task is not None and not response_task.done():
                response_task.add_done_callback(consume_task_result)

"""Microphone Capture — real-time audio input with VAD integration.

Supports:
- Recording from default microphone
- Voice Activity Detection with pre-roll buffer
- Audio chunk streaming for ASR pipeline
- Configurable sample rate and chunk size
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import logging
from dataclasses import dataclass
from typing import Any

from companion.audio.vad import VADConfig, VADResult, VoiceActivityDetector

logger = logging.getLogger(__name__)


@dataclass
class MicConfig:
    """Configuration for microphone capture."""

    sample_rate: int = 16000
    channels: int = 1
    chunk_duration_ms: int = 20  # Frame size for VAD
    device_index: int | None = None  # None = system default
    pre_roll_buffer_ms: int = 400  # Buffer before VAD trigger
    max_speech_duration_ms: int = 30_000  # Auto-stop after 30s
    silence_duration_ms: int = 800  # Silence before auto-end of speech
    startup_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.sample_rate not in {8000, 16000, 24000, 48000}:
            raise ValueError("microphone sample_rate is unsupported")
        if self.channels != 1:
            raise ValueError("only mono microphone capture is supported")
        if self.chunk_duration_ms not in {10, 20, 30}:
            raise ValueError("chunk_duration_ms must be 10, 20, or 30")
        if not 0 <= self.pre_roll_buffer_ms <= 2000:
            raise ValueError("pre_roll_buffer_ms must be between 0 and 2000")
        if self.max_speech_duration_ms <= 0 or self.silence_duration_ms <= 0:
            raise ValueError("microphone duration limits must be positive")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("microphone startup_timeout_seconds must be positive")


@dataclass
class MicState:
    """Current state of the microphone capture."""

    is_capturing: bool = False
    is_speaking: bool = False
    speech_started_at: float = 0.0
    speech_ended_at: float = 0.0
    total_frames_captured: int = 0
    total_speech_frames: int = 0


class MicrophoneCapture:
    """Asynchronous microphone capture with VAD.

    Captures audio frames and detects speech using the energy-based VAD.
    Collected speech audio (with pre-roll buffer) is streamed to the
    ASR provider for real-time transcription.
    """

    def __init__(self, config: MicConfig | None = None) -> None:
        self._config = config or MicConfig()
        self._vad = VoiceActivityDetector(
            VADConfig(
                sample_rate=self._config.sample_rate,
                frame_duration_ms=self._config.chunk_duration_ms,
                pre_roll_buffer_ms=self._config.pre_roll_buffer_ms,
                max_speech_duration_ms=self._config.max_speech_duration_ms,
                speech_end_frames=int(
                    self._config.silence_duration_ms / self._config.chunk_duration_ms
                ),
            )
        )
        self._state = MicState()
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._speech_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._running: bool = False
        self._capture_task: asyncio.Task[None] | None = None
        self._ready_event = asyncio.Event()
        self._startup_error: BaseException | None = None

    @property
    def state(self) -> MicState:
        return self._state

    @property
    def vad(self) -> VoiceActivityDetector:
        return self._vad

    async def start(self) -> bool:
        """Start microphone capture. Returns True if microphone is available."""
        if (
            importlib.util.find_spec("pyaudio") is None
            and importlib.util.find_spec("sounddevice") is None
        ):
            logger.error("No microphone backend installed; install virtual-companion[voice]")
            return False
        try:
            self._ready_event.clear()
            self._startup_error = None
            self._running = True
            self._state.is_capturing = True
            self._capture_task = asyncio.create_task(self._capture_loop_guarded())
            await asyncio.wait_for(
                self._ready_event.wait(), timeout=self._config.startup_timeout_seconds
            )
            if self._startup_error is not None or not self._running:
                error_name = (
                    type(self._startup_error).__name__ if self._startup_error else "unknown error"
                )
                logger.error("Microphone stream failed to start: %s", error_name)
                await self.stop()
                return False
            logger.info("Microphone capture started")
            return True
        except TimeoutError:
            logger.error("Microphone stream startup timed out")
            await self.stop()
            return False
        except Exception:
            logger.exception("Failed to start microphone capture")
            self._running = False
            await self.stop()
            return False

    async def stop(self) -> None:
        """Stop microphone capture."""
        self._running = False
        self._state.is_capturing = False
        if self._capture_task:
            self._capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._capture_task
            self._capture_task = None
        self._vad.reset()
        logger.info("Microphone capture stopped")

    async def _capture_loop_guarded(self) -> None:
        try:
            await self._capture_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._startup_error = exc
            logger.exception("Microphone capture stopped unexpectedly")
        finally:
            self._running = False
            self._state.is_capturing = False
            self._ready_event.set()

    async def get_speech_audio(self, timeout: float = 15.0) -> bytes | None:
        """Wait for and collect a speech utterance.

        Blocks until speech is detected, completes, and returns collected audio.
        Returns None if timeout is exceeded.
        """
        audio_chunks: list[bytes] = []

        start_time = asyncio.get_event_loop().time()
        speech_active = False

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return b"".join(audio_chunks) if audio_chunks else None

            try:
                chunk = await asyncio.wait_for(self._speech_queue.get(), timeout=0.5)
                if not chunk:
                    if speech_active:
                        return b"".join(audio_chunks)
                    continue
                audio_chunks.append(chunk)
                speech_active = True
            except TimeoutError:
                if speech_active:
                    # Speech ended (no more audio after silence threshold)
                    return b"".join(audio_chunks)

    async def _capture_loop(self) -> None:
        """Background loop: captures audio, runs VAD, and queues speech chunks.

        In a real implementation, this would read from pyaudio/sounddevice.
        For Phase 2, we simulate by feeding a test audio source.
        The actual pyaudio capture is added when the dependency is installed.
        """
        if importlib.util.find_spec("pyaudio") is not None:
            await self._capture_pyaudio()
        elif importlib.util.find_spec("sounddevice") is not None:
            await self._capture_sounddevice()
        else:
            logger.info(
                "No audio capture library found (pyaudio/sounddevice). "
                "Install pyaudio for voice input: pip install pyaudio"
            )
            self._running = False
            self._state.is_capturing = False
            self._startup_error = RuntimeError("No microphone backend installed")
            self._ready_event.set()

    async def _capture_pyaudio(self) -> None:
        """Capture using PyAudio (cross-platform, most common)."""
        pyaudio = importlib.import_module("pyaudio")

        p = pyaudio.PyAudio()
        chunk_size = int(self._config.sample_rate * self._config.chunk_duration_ms / 1000)

        try:
            stream = p.open(
                format=pyaudio.paInt16,
                channels=self._config.channels,
                rate=self._config.sample_rate,
                input=True,
                input_device_index=self._config.device_index,
                frames_per_buffer=chunk_size,
            )
            self._ready_event.set()

            while self._running:
                try:
                    audio_bytes = await asyncio.to_thread(
                        stream.read, chunk_size, exception_on_overflow=False
                    )
                except Exception:
                    continue

                # Run VAD
                result: VADResult = self._vad.process_frame(audio_bytes)
                self._state.total_frames_captured += 1

                await self._handle_vad_frame(audio_bytes, result)

                await asyncio.sleep(0)  # Yield to event loop

        finally:
            stream.close()
            p.terminate()

    async def _capture_sounddevice(self) -> None:
        """Capture using sounddevice (alternative)."""
        sd = importlib.import_module("sounddevice")

        chunk_size = int(self._config.sample_rate * self._config.chunk_duration_ms / 1000)

        loop = asyncio.get_running_loop()

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                logger.warning("Audio input status: %s", status)
            audio_bytes = indata.tobytes()
            loop.call_soon_threadsafe(self._enqueue_audio_frame, audio_bytes)

        with sd.InputStream(
            samplerate=self._config.sample_rate,
            channels=self._config.channels,
            dtype="int16",
            blocksize=chunk_size,
            callback=callback,
            device=self._config.device_index,
        ):
            self._ready_event.set()
            while self._running:
                try:
                    audio_bytes = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                result = self._vad.process_frame(audio_bytes)
                self._state.total_frames_captured += 1

                await self._handle_vad_frame(audio_bytes, result)

    def _enqueue_audio_frame(self, audio_bytes: bytes) -> None:
        try:
            self._audio_queue.put_nowait(audio_bytes)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._audio_queue.put_nowait(audio_bytes)

    async def _handle_vad_frame(self, audio_bytes: bytes, result: VADResult) -> None:
        if result.speech_detected:
            self._state.is_speaking = True
            self._state.total_speech_frames += 1
            if result.speech_started:
                self._state.speech_started_at = asyncio.get_event_loop().time()
                # The detector's pre-roll already ends with this frame, so do
                # not enqueue the current frame a second time.
                await self._speech_queue.put(self._vad.get_pre_roll_audio())
            else:
                await self._speech_queue.put(audio_bytes)
        if result.speech_ended:
            self._state.is_speaking = False
            self._state.speech_ended_at = asyncio.get_event_loop().time()
            await self._speech_queue.put(b"")


class VoiceChatMode:
    """Voice-activated chat loop using the full audio pipeline.

    Usage:
        mic = MicrophoneCapture()
        await mic.start()
        chat = VoiceChatMode(mic, voice_pipeline)
        await chat.run()
    """

    def __init__(
        self,
        mic: MicrophoneCapture,
        voice_pipeline: Any,
        companion_name: str = "虚拟伙伴",
    ) -> None:
        self._mic = mic
        self._pipeline = voice_pipeline
        self._companion_name = companion_name
        self._running = False

    async def run(self) -> None:
        """Run the voice chat loop. Press Ctrl+C to exit."""
        self._running = True
        print("\n🎤 语音模式 — 开始说话吧（Ctrl+C 退出）\n")
        speech_task: asyncio.Task[bytes | None] | None = asyncio.create_task(
            self._mic.get_speech_audio(timeout=30.0)
        )
        response_task: asyncio.Task[str] | None = None
        try:
            while self._running:
                print("👂 正在听…", end="\r")
                if speech_task is None:
                    break
                audio = await speech_task
                speech_task = asyncio.create_task(self._mic.get_speech_audio(timeout=30.0))
                if not audio or len(audio) < 1600:  # < 100ms
                    continue
                print("🎤 正在转录…", end="\r")
                response_task = asyncio.create_task(self._pipeline.process_audio_input(audio))

                # Keep listening during synthesis/playback. New user speech
                # cancels the current response and immediately becomes the next turn.
                while self._running:
                    done, _ = await asyncio.wait(
                        {response_task, speech_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if response_task in done:
                        response = response_task.result()
                        print("                       ", end="\r")
                        if response and not response.startswith("["):
                            print(f"{self._companion_name}: {response}")
                        elif response:
                            print(f"⚠ {response}")
                        response_task = None
                        break

                    next_audio = speech_task.result()
                    speech_task = asyncio.create_task(self._mic.get_speech_audio(timeout=30.0))
                    if not next_audio or len(next_audio) < 1600:
                        continue
                    await self._pipeline.interrupt()
                    await response_task
                    response_task = asyncio.create_task(
                        self._pipeline.process_audio_input(next_audio)
                    )

        except KeyboardInterrupt:
            print("\n👋 语音模式结束")
        finally:
            self._running = False
            for task in (speech_task, response_task):
                if task and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

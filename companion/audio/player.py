"""Audio Output — play synthesized speech through system audio.

Supports:
- Playing WAV/PCM audio bytes via system default audio player
- Saving audio to temp file and playing
- Streaming audio chunks for low-latency playback
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybackResult:
    """Measured result of one audio playback operation."""

    played_duration_ms: int
    was_interrupted: bool = False
    stream_generation: int = 0
    output_underflow: bool = False
    started_at_ms: int = 0
    started_at_ns: int = 0


class SystemAudioOutput:
    """Single-stream system audio output with cooperative cancellation."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._interrupted = False
        self._stream_generation = 0

    async def play(self, pcm_data: bytes, sample_rate: int = 24_000) -> PlaybackResult:
        if not pcm_data:
            return PlaybackResult(played_duration_ms=0)

        await self.stop()
        self._interrupted = False
        wav_data = pcm_to_wav(pcm_data, sample_rate)
        expected_duration_ms = int(len(pcm_data) / (sample_rate * 2) * 1000)
        with tempfile.NamedTemporaryFile(
            prefix="companion_tts_", suffix=".wav", delete=False
        ) as temp_file:
            temp_file.write(wav_data)
            temp_path = temp_file.name
        try:
            started_at = time.monotonic()
            playback_started_at_ms = int(time.time() * 1000)
            playback_started_at_ns = time.perf_counter_ns()
            self._stream_generation += 1
            stream_generation = self._stream_generation
            self._process = await self._start_process(temp_path)
            await self._process.wait()
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            played_ms = min(expected_duration_ms, elapsed_ms)
            if not self._interrupted and self._process.returncode == 0:
                played_ms = expected_duration_ms
            return PlaybackResult(
                played_duration_ms=played_ms,
                was_interrupted=self._interrupted,
                stream_generation=stream_generation,
                started_at_ms=playback_started_at_ms,
                started_at_ns=playback_started_at_ns,
            )
        finally:
            self._process = None
            with contextlib.suppress(OSError):
                os.unlink(temp_path)

    async def stop(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        self._interrupted = True
        process.terminate()
        with contextlib.suppress(ProcessLookupError, TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=0.25)
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()

    async def finish(self) -> None:
        """System playback is complete when ``play`` returns."""

    @staticmethod
    async def _start_process(wav_path: str) -> asyncio.subprocess.Process:
        if os.name == "nt":
            return await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                f'(New-Object Media.SoundPlayer "{wav_path}").PlaySync()',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return await asyncio.create_subprocess_exec(
            "ffplay",
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "quiet",
            wav_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )


class SoundDeviceAudioOutput:
    """Gapless raw PCM output for streaming voice responses."""

    def __init__(self) -> None:
        self._stream: Any | None = None
        self._sample_rate = 0
        self._interrupted = False
        self._stream_generation = 0
        self._active_stream_generation = 0

    async def play(self, pcm_data: bytes, sample_rate: int = 24_000) -> PlaybackResult:
        if not pcm_data:
            return PlaybackResult(played_duration_ms=0)
        await self._ensure_stream(sample_rate)
        self._interrupted = False
        duration_ms = int(len(pcm_data) / (sample_rate * 2) * 1000)
        stream = self._stream
        if stream is None:
            raise RuntimeError("Audio stream was not initialized")
        stream_generation = self._active_stream_generation
        playback_started_at_ms = 0
        playback_started_at_ns = 0

        def write_pcm() -> Any:
            nonlocal playback_started_at_ms, playback_started_at_ns
            playback_started_at_ms = int(time.time() * 1000)
            playback_started_at_ns = time.perf_counter_ns()
            return stream.write(pcm_data)

        try:
            output_underflow = bool(await asyncio.to_thread(write_pcm))
        except Exception:
            if not self._interrupted:
                raise
            return PlaybackResult(
                played_duration_ms=0,
                was_interrupted=True,
                stream_generation=stream_generation,
                started_at_ms=playback_started_at_ms,
                started_at_ns=playback_started_at_ns,
            )
        return PlaybackResult(
            played_duration_ms=duration_ms,
            was_interrupted=self._interrupted,
            stream_generation=stream_generation,
            output_underflow=output_underflow,
            started_at_ms=playback_started_at_ms,
            started_at_ns=playback_started_at_ns,
        )

    async def _ensure_stream(self, sample_rate: int) -> None:
        if self._stream is not None and self._sample_rate == sample_rate:
            return
        await self.finish()
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as exc:
            raise RuntimeError("Streaming playback requires virtual-companion[voice]") from exc
        self._stream = sounddevice.RawOutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        await asyncio.to_thread(self._stream.start)
        self._sample_rate = sample_rate
        self._stream_generation += 1
        self._active_stream_generation = self._stream_generation

    async def finish(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        self._sample_rate = 0
        self._active_stream_generation = 0
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.stop)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)

    async def stop(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._interrupted = True
        self._stream = None
        self._sample_rate = 0
        self._active_stream_generation = 0
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.abort)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)


# PCM WAV header template; size fields are patched per call in pcm_to_wav.
_PCM_HEADER_TEMPLATE: bytes = (
    b"RIFF"  # ChunkID
    b"\x00\x00\x00\x00"  # ChunkSize (placeholder)
    b"WAVE"  # Format
    b"fmt "  # Subchunk1ID
    b"\x10\x00\x00\x00"  # Subchunk1Size (16 for PCM)
    b"\x01\x00"  # AudioFormat (1 = PCM)
    b"\x01\x00"  # NumChannels (1 = mono)
    b"\x00\x00\x00\x00"  # SampleRate (placeholder)
    b"\x00\x00\x00\x00"  # ByteRate (placeholder)
    b"\x02\x00"  # BlockAlign (placeholder)
    b"\x10\x00"  # BitsPerSample (16)
    b"data"  # Subchunk2ID
    b"\x00\x00\x00\x00"  # Subchunk2Size (placeholder)
)


def pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int = 24000,
    num_channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Wrap raw PCM audio bytes in a WAV container.

    This is needed because the cloud TTS provider returns raw PCM, but most
    system audio players expect a WAV container.
    """
    data_size = len(pcm_data)
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8

    header = bytearray(_PCM_HEADER_TEMPLATE)

    # ChunkSize (file size - 8)
    file_size = 36 + data_size
    header[4:8] = file_size.to_bytes(4, "little")
    # SampleRate
    header[24:28] = sample_rate.to_bytes(4, "little")
    # ByteRate
    header[28:32] = byte_rate.to_bytes(4, "little")
    # BlockAlign
    header[32:34] = block_align.to_bytes(2, "little")
    # BitsPerSample
    header[34:36] = bits_per_sample.to_bytes(2, "little")
    # Subchunk2Size
    header[40:44] = data_size.to_bytes(4, "little")

    return bytes(header) + pcm_data

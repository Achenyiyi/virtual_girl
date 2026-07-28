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
import subprocess
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


class SystemAudioOutput:
    """Single-stream system audio output with cooperative cancellation."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._interrupted = False

    async def play(self, pcm_data: bytes, sample_rate: int = 24_000) -> PlaybackResult:
        if not pcm_data:
            return PlaybackResult(played_duration_ms=0)

        await self.stop()
        self._interrupted = False
        wav_data = AudioPlayer.pcm_to_wav(pcm_data, sample_rate)
        expected_duration_ms = int(len(pcm_data) / (sample_rate * 2) * 1000)
        with tempfile.NamedTemporaryFile(
            prefix="companion_tts_", suffix=".wav", delete=False
        ) as temp_file:
            temp_file.write(wav_data)
            temp_path = temp_file.name
        try:
            started_at = time.monotonic()
            self._process = await self._start_process(temp_path)
            await self._process.wait()
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            played_ms = min(expected_duration_ms, elapsed_ms)
            if not self._interrupted and self._process.returncode == 0:
                played_ms = expected_duration_ms
            return PlaybackResult(
                played_duration_ms=played_ms,
                was_interrupted=self._interrupted,
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

    async def play(self, pcm_data: bytes, sample_rate: int = 24_000) -> PlaybackResult:
        if not pcm_data:
            return PlaybackResult(played_duration_ms=0)
        await self._ensure_stream(sample_rate)
        self._interrupted = False
        duration_ms = int(len(pcm_data) / (sample_rate * 2) * 1000)
        stream = self._stream
        if stream is None:
            raise RuntimeError("Audio stream was not initialized")
        try:
            await asyncio.to_thread(stream.write, pcm_data)
        except Exception:
            if not self._interrupted:
                raise
            return PlaybackResult(played_duration_ms=0, was_interrupted=True)
        return PlaybackResult(
            played_duration_ms=duration_ms,
            was_interrupted=self._interrupted,
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

    async def finish(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        self._sample_rate = 0
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
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.abort)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)


class AudioPlayer:
    """Play audio through the system's default audio output.

    On Windows, uses PowerShell's System.Media.SoundPlayer or
    falls back to writing a WAV file and using the default player.
    """

    # PCM format constants
    PCM_HEADER_TEMPLATE: bytes = (
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

    @classmethod
    def pcm_to_wav(
        cls,
        pcm_data: bytes,
        sample_rate: int = 24000,
        num_channels: int = 1,
        bits_per_sample: int = 16,
    ) -> bytes:
        """Wrap raw PCM audio bytes in a WAV container.

        This is needed because Azure TTS returns raw PCM, but most
        system audio players expect a WAV container.
        """
        data_size = len(pcm_data)
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8

        header = bytearray(cls.PCM_HEADER_TEMPLATE)

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

    @classmethod
    async def play_pcm(
        cls,
        pcm_data: bytes,
        sample_rate: int = 24000,
        blocking: bool = False,
    ) -> bool:
        """Play raw PCM audio bytes.

        Args:
            pcm_data: Raw 16-bit mono PCM audio
            sample_rate: Audio sample rate (default 24000 for Azure TTS)
            blocking: If True, wait for playback to finish

        Returns:
            True if playback started successfully
        """
        if not pcm_data:
            logger.warning("No audio data to play")
            return False

        # Wrap PCM in WAV container
        wav_data = cls.pcm_to_wav(pcm_data, sample_rate)

        # Write to temp file (cleanup handled by OS on next boot if we crash)
        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, f"companion_tts_{hash(pcm_data) & 0xFFFF}.wav")
        with open(tmp_path, "wb") as f:
            f.write(wav_data)

        try:
            if blocking:
                await cls._play_blocking(tmp_path)
            else:
                await cls._play_async(tmp_path)
            return True
        except Exception:
            logger.exception("Audio playback failed")
            return False

    @classmethod
    async def _play_async(cls, wav_path: str) -> None:
        """Play audio asynchronously (non-blocking)."""
        if os.name == "nt":
            # Windows: use PowerShell to play
            await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                f'(New-Object Media.SoundPlayer "{wav_path}").PlaySync()',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Don't await — fire and forget
        else:
            # macOS / Linux fallback
            subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    @classmethod
    async def _play_blocking(cls, wav_path: str) -> None:
        """Play audio synchronously (block until done)."""
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                f'(New-Object Media.SoundPlayer "{wav_path}").PlaySync()',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        else:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
                capture_output=True,
            )

    @classmethod
    async def say(
        cls,
        text: str,
        tts_provider: Any = None,  # Optional TTSProvider for synthesis
        sample_rate: int = 24000,
    ) -> bool:
        """Convenience: synthesize and play text as speech.

        If tts_provider is None, logs the text without playing.
        """
        if tts_provider is None:
            logger.info("TTS not available, text: %s", text[:50])
            return False

        from companion.providers.tts import TTSRequest

        request = TTSRequest(text=text, turn_id="direct_tts", sample_rate=sample_rate)
        try:
            chunk = await tts_provider.synthesize(request)
            if chunk.audio_bytes:
                return await cls.play_pcm(chunk.audio_bytes, sample_rate)
        except Exception:
            logger.exception("TTS synthesis failed")
        return False

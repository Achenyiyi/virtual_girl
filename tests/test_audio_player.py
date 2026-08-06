"""Regression tests for standards-compliant audio output."""

from __future__ import annotations

import asyncio
import io
import wave

import pytest

from companion.audio.microphone import MicrophoneCapture, VoiceChatMode
from companion.audio.player import SoundDeviceAudioOutput, SystemAudioOutput, pcm_to_wav


def test_pcm_to_wav_produces_standard_header() -> None:
    pcm = b"\x00\x00" * 800
    wav_data = pcm_to_wav(pcm, sample_rate=16_000)

    assert len(wav_data) == 44 + len(pcm)
    with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnframes() == 800


@pytest.mark.asyncio
async def test_system_audio_output_can_be_stopped(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.done = asyncio.Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15
            self.done.set()

        def kill(self) -> None:
            self.returncode = -9
            self.done.set()

    process = FakeProcess()

    async def start_process(_path):
        return process

    monkeypatch.setattr(SystemAudioOutput, "_start_process", staticmethod(start_process))
    output = SystemAudioOutput()
    play_task = asyncio.create_task(output.play(b"\x00\x00" * 16_000, 16_000))
    while output._process is None:
        await asyncio.sleep(0)
    await output.stop()
    result = await play_task
    assert result.was_interrupted
    assert process.returncode == -15


@pytest.mark.asyncio
async def test_microphone_start_reports_missing_backend(monkeypatch) -> None:
    monkeypatch.setattr("companion.audio.microphone.importlib.util.find_spec", lambda _name: None)
    microphone = MicrophoneCapture()
    assert not await microphone.start()
    assert not microphone.state.is_capturing


@pytest.mark.asyncio
async def test_microphone_start_waits_for_stream_readiness(monkeypatch) -> None:
    microphone = MicrophoneCapture()

    async def ready_capture() -> None:
        microphone._ready_event.set()
        while microphone._running:
            await asyncio.sleep(0)

    monkeypatch.setattr(microphone, "_capture_sounddevice", ready_capture)
    monkeypatch.setattr(
        "companion.audio.microphone.importlib.util.find_spec",
        lambda name: object() if name == "sounddevice" else None,
    )

    assert await microphone.start()
    assert microphone.state.is_capturing
    await microphone.stop()


@pytest.mark.asyncio
async def test_microphone_start_reports_stream_open_failure(monkeypatch) -> None:
    microphone = MicrophoneCapture()

    async def failed_capture() -> None:
        raise OSError("device unavailable")

    monkeypatch.setattr(microphone, "_capture_sounddevice", failed_capture)
    monkeypatch.setattr(
        "companion.audio.microphone.importlib.util.find_spec",
        lambda name: object() if name == "sounddevice" else None,
    )

    assert not await microphone.start()
    assert not microphone.state.is_capturing


@pytest.mark.asyncio
async def test_sounddevice_output_reuses_stream_for_gapless_chunks(monkeypatch) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.writes = []
            self.started = False
            self.stopped = False
            self.closed = False

        def start(self) -> None:
            self.started = True

        def write(self, data: bytes) -> None:
            self.writes.append(data)
            return False

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

        def abort(self) -> None:
            self.stopped = True

    stream = FakeStream()
    module = type("FakeSoundDevice", (), {"RawOutputStream": lambda **_kwargs: stream})
    monkeypatch.setattr("companion.audio.player.importlib.import_module", lambda _name: module)
    output = SoundDeviceAudioOutput()

    first = await output.play(b"\x00\x00" * 100, 100)
    second = await output.play(b"\x01\x00" * 100, 100)
    await output.finish()

    assert first.played_duration_ms == second.played_duration_ms == 1000
    assert first.stream_generation == second.stream_generation == 1
    assert not first.output_underflow and not second.output_underflow
    assert stream.started
    assert len(stream.writes) == 2
    assert stream.stopped and stream.closed


@pytest.mark.asyncio
async def test_sounddevice_output_reports_underflow(monkeypatch) -> None:
    class UnderflowStream:
        def start(self) -> None:
            return None

        def write(self, _data: bytes) -> bool:
            return True

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    stream = UnderflowStream()
    module = type("FakeSoundDevice", (), {"RawOutputStream": lambda **_kwargs: stream})
    monkeypatch.setattr("companion.audio.player.importlib.import_module", lambda _name: module)
    output = SoundDeviceAudioOutput()

    result = await output.play(b"\x00\x00" * 100, 100)
    await output.finish()

    assert result.stream_generation == 1
    assert result.output_underflow


@pytest.mark.asyncio
async def test_vad_queues_preroll_without_duplicating_trigger_frame() -> None:
    microphone = MicrophoneCapture()
    silence = b"\x00\x00" * 320
    speech_sample = (10_000).to_bytes(2, "little", signed=True)
    speech = speech_sample * 320

    for _ in range(3):
        result = microphone.vad.process_frame(silence)
        await microphone._handle_vad_frame(silence, result)
    for _ in range(5):
        result = microphone.vad.process_frame(speech)
        await microphone._handle_vad_frame(speech, result)

    pre_roll = microphone._speech_queue.get_nowait()
    assert len(pre_roll) == 8 * len(speech)
    assert pre_roll.endswith(speech)
    assert microphone._speech_queue.empty()


@pytest.mark.asyncio
async def test_voice_chat_interrupts_on_new_vad_edge_before_utterance_finishes() -> None:
    class EdgeMicrophone:
        def __init__(self) -> None:
            self.speech_start_sequence = 1
            self._second_started = asyncio.Event()
            self._second_finished = asyncio.Event()
            self._calls = 0

        async def get_speech_audio(self, timeout: float = 15.0) -> bytes | None:
            del timeout
            self._calls += 1
            if self._calls == 1:
                return b"a" * 3200
            self.speech_start_sequence = 2
            self._second_started.set()
            await self._second_finished.wait()
            return b"b" * 3200

        async def wait_for_speech_start(
            self, after_sequence: int, timeout: float
        ) -> int | None:
            assert after_sequence == 1
            await asyncio.wait_for(self._second_started.wait(), timeout=timeout)
            return self.speech_start_sequence

    class EdgePipeline:
        def __init__(self, microphone: EdgeMicrophone) -> None:
            self._microphone = microphone
            self.interrupted = asyncio.Event()
            self._released = asyncio.Event()

        async def process_audio_input(self, audio: bytes) -> str:
            assert audio
            if not self.interrupted.is_set():
                await self._released.wait()
            return "response"

        async def interrupt(self) -> bool:
            assert not self._microphone._second_finished.is_set()
            self.interrupted.set()
            self._released.set()
            return True

    microphone = EdgeMicrophone()
    pipeline = EdgePipeline(microphone)
    mode = VoiceChatMode(microphone, pipeline)
    run_task = asyncio.create_task(mode.run())
    try:
        await asyncio.wait_for(pipeline.interrupted.wait(), timeout=0.5)
        assert microphone.speech_start_sequence == 2
    finally:
        microphone._second_finished.set()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

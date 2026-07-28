from __future__ import annotations

import asyncio
from argparse import Namespace
from typing import Any

import pytest

from companion.__main__ import CompanionApp, async_main
from companion.config_loader import RuntimeConfig


class LifecycleComponent:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        fail: bool = False,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.fail = fail
        self.entered = entered
        self.release = release

    async def stop(self) -> None:
        await self._close()

    async def shutdown(self) -> None:
        await self._close()

    async def _close(self) -> None:
        self.calls.append(self.name)
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


def _inject_lifecycle_components(
    app: CompanionApp,
    calls: list[str],
    *,
    failing: str | None = None,
    blocking: tuple[asyncio.Event, asyncio.Event] | None = None,
) -> None:
    entered, release = blocking or (None, None)
    app._audio_output = LifecycleComponent(
        "system_audio",
        calls,
        fail=failing == "system_audio",
        entered=entered if blocking else None,
        release=release if blocking else None,
    )
    app._voice_audio_output = LifecycleComponent("streaming_audio", calls)
    app._voice_pipeline = LifecycleComponent("voice_pipeline", calls)
    app._bus = LifecycleComponent("event_bus", calls)
    app._orchestrator = LifecycleComponent("orchestrator", calls)
    app._action_audit = LifecycleComponent("action_audit", calls)


class OrderedEventBus(LifecycleComponent):
    def __init__(self, calls: list[str], drained: asyncio.Event) -> None:
        super().__init__("event_bus", calls)
        self.drained = drained

    async def shutdown(self) -> None:
        self.calls.append(self.name)
        self.drained.set()


class OrderedOrchestrator(LifecycleComponent):
    def __init__(self, calls: list[str], drained: asyncio.Event) -> None:
        super().__init__("orchestrator", calls)
        self.drained = drained

    async def shutdown(self) -> None:
        assert self.drained.is_set()
        self.calls.append(self.name)


@pytest.mark.asyncio
async def test_stop_attempts_every_component_and_is_idempotent() -> None:
    app = CompanionApp(RuntimeConfig())
    calls: list[str] = []
    _inject_lifecycle_components(app, calls, failing="system_audio")

    await asyncio.gather(app.stop(), app.stop())
    await app.stop()

    assert sorted(calls) == sorted(
        [
            "system_audio",
            "streaming_audio",
            "voice_pipeline",
            "event_bus",
            "orchestrator",
            "action_audit",
        ]
    )


@pytest.mark.asyncio
async def test_cancelled_stop_waits_for_cleanup_before_propagating() -> None:
    app = CompanionApp(RuntimeConfig())
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    _inject_lifecycle_components(app, calls, blocking=(entered, release))

    stop_task = asyncio.create_task(app.stop())
    await entered.wait()
    stop_task.cancel()
    await asyncio.sleep(0)
    assert not stop_task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert sorted(calls) == sorted(
        [
            "system_audio",
            "streaming_audio",
            "voice_pipeline",
            "event_bus",
            "orchestrator",
            "action_audit",
        ]
    )


@pytest.mark.asyncio
async def test_hung_component_is_bounded_and_does_not_block_other_cleanup(monkeypatch) -> None:
    app = CompanionApp(RuntimeConfig())
    calls: list[str] = []
    never_release = asyncio.Event()
    entered = asyncio.Event()
    _inject_lifecycle_components(app, calls, blocking=(entered, never_release))
    monkeypatch.setattr("companion.__main__._SHUTDOWN_STEP_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(app.stop(), timeout=1)

    assert sorted(calls) == sorted(
        [
            "system_audio",
            "streaming_audio",
            "voice_pipeline",
            "event_bus",
            "orchestrator",
            "action_audit",
        ]
    )


@pytest.mark.asyncio
async def test_event_bus_drains_before_provider_shutdown() -> None:
    app = CompanionApp(RuntimeConfig())
    calls: list[str] = []
    drained = asyncio.Event()
    _inject_lifecycle_components(app, calls)
    app._bus = OrderedEventBus(calls, drained)
    app._orchestrator = OrderedOrchestrator(calls, drained)

    await app.stop()

    assert calls.index("event_bus") < calls.index("orchestrator")


@pytest.mark.asyncio
async def test_once_failure_still_stops_application(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "companion.yaml"
    config_path.write_text(
        "providers:\n"
        "  llm:\n"
        "    type: cloud\n"
        "    cloud:\n"
        "      provider: openai_compatible\n"
        "      model: test-model\n"
        "      api_key_env: TEST_LIFECYCLE_KEY\n"
        "      base_url: https://example.invalid/v1/chat/completions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_LIFECYCLE_KEY", "x" * 32)
    stopped = asyncio.Event()

    async def start(_self) -> bool:
        return True

    async def fail_chat(_self, _message: str, speak: bool = False) -> dict[str, Any]:
        del speak
        raise RuntimeError("request failed")

    async def stop(_self) -> None:
        stopped.set()

    monkeypatch.setattr(CompanionApp, "start", start)
    monkeypatch.setattr(CompanionApp, "chat", fail_chat)
    monkeypatch.setattr(CompanionApp, "stop", stop)

    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once="hello",
    )
    with pytest.raises(RuntimeError, match="request failed"):
        await async_main(args)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_startup_exception_still_stops_application(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "companion.yaml"
    config_path.write_text(
        "providers:\n"
        "  llm:\n"
        "    type: cloud\n"
        "    cloud:\n"
        "      provider: openai_compatible\n"
        "      model: test-model\n"
        "      api_key_env: TEST_STARTUP_KEY\n"
        "      base_url: https://example.invalid/v1/chat/completions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_STARTUP_KEY", "x" * 32)
    stopped = asyncio.Event()

    async def fail_start(_self) -> bool:
        raise RuntimeError("startup failed")

    async def stop(_self) -> None:
        stopped.set()

    monkeypatch.setattr(CompanionApp, "start", fail_start)
    monkeypatch.setattr(CompanionApp, "stop", stop)

    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        await async_main(args)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_microphone_shutdown_failure_still_stops_application(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "companion.yaml"
    config_path.write_text(
        "providers:\n"
        "  llm:\n"
        "    type: cloud\n"
        "    cloud:\n"
        "      provider: openai_compatible\n"
        "      model: test-model\n"
        "      api_key_env: TEST_VOICE_KEY\n"
        "      base_url: https://example.invalid/v1/chat/completions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_VOICE_KEY", "x" * 32)
    stopped = asyncio.Event()

    async def start(_self) -> bool:
        return True

    async def start_voice(_self) -> bool:
        return True

    async def stop(_self) -> None:
        stopped.set()

    class FailingMicrophone:
        def __init__(self, _config) -> None:
            pass

        async def start(self) -> bool:
            return True

        async def stop(self) -> None:
            raise RuntimeError("microphone close failed")

    async def run_voice(_self) -> None:
        return None

    monkeypatch.setattr(CompanionApp, "start", start)
    monkeypatch.setattr(CompanionApp, "start_voice_mode", start_voice)
    monkeypatch.setattr(CompanionApp, "stop", stop)
    monkeypatch.setattr("companion.__main__.MicrophoneCapture", FailingMicrophone)
    monkeypatch.setattr("companion.__main__.VoiceChatMode.run", run_voice)

    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=True,
        voice=False,
        once=None,
    )
    with pytest.raises(RuntimeError, match="microphone close failed"):
        await async_main(args)
    assert stopped.is_set()

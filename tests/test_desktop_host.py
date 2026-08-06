from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from companion.config_loader import RuntimeConfig
from companion.desktop.control_protocol import ControlError
from companion.desktop.host import DesktopHost
from companion.events.conversation import (
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
    ConversationTurnInterruptedEvent,
    ConversationTurnStartedEvent,
)
from companion.providers.implementations.cloud_llm import CloudLLMConfig
from companion.providers.implementations.cloud_tts import CloudTTSConfig
from companion.services.avatar_stage_supervisor import (
    AvatarStageLaunchConfig,
    _build_child_environment,
)


class FakeMemory:
    async def query_events(self, _query):
        return []


class FakeBus:
    def on_any(self):
        return lambda handler: handler


@dataclass
class FakeIdentity:
    name: str = "小凛"
    avatar_model_id: str = "model"


@dataclass
class FakeAffect:
    valence: float = 0.0
    arousal: float = 0.5
    energy: float = 0.5


class FakeState:
    identity = FakeIdentity()
    affect = FakeAffect()

    def dominant_emotion(self) -> str:
        return "neutral"


class FakePipeline:
    current_session_id = "session-current"
    active_turn_id = ""

    def __init__(self) -> None:
        self.assistant_delta_callback = None

    def set_assistant_delta_callback(self, callback) -> None:
        self.assistant_delta_callback = callback


class FakeApp:
    def __init__(self) -> None:
        self.memory = FakeMemory()
        self.event_bus = FakeBus()
        self.state = FakeState()
        self.voice_pipeline = FakePipeline()
        self._orchestrator = object()
        self._llm = object()
        self._tts = object()
        self._asr = object()
        self._avatar = object()
        self._action_provider = None
        self._avatar_stage = None

    async def prepare_desktop_voice(self) -> bool:
        return True


def _desktop_launch_config() -> AvatarStageLaunchConfig:
    return AvatarStageLaunchConfig(
        executable_path="C:/AIRI/airi.exe",
        expected_sha256="a" * 64,
        expected_app_asar_sha256="b" * 64,
        expected_godot_sha256="c" * 64,
        model_path="C:/AIRI/model.vrm",
        expected_model_sha256="d" * 64,
        model_id="managed-model",
        startup_timeout_seconds=1.0,
    )


def test_snapshot_returns_only_credential_source_states(monkeypatch) -> None:
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(
            api_key_env="DESKTOP_LLM_KEY",
            credential_target="VirtualCompanion/LLM",
        ),
        tts_config=CloudTTSConfig(
            credential_target="VirtualCompanion/TTS",
        ),
    )
    monkeypatch.setenv("DESKTOP_LLM_KEY", "super-secret")
    monkeypatch.setattr(
        "companion.desktop.host.read_windows_credential",
        lambda target: "tts-secret" if target.endswith("TTS") else "",
    )
    host = DesktopHost(FakeApp(), config)

    snapshot = host._snapshot()

    assert snapshot["credentials"] == {
        "llm": "environment",
        "tts": "windows_credential",
    }
    assert "super-secret" not in str(snapshot)
    assert "tts-secret" not in str(snapshot)


@pytest.mark.asyncio
async def test_environment_override_blocks_credential_writes(monkeypatch) -> None:
    config = RuntimeConfig(
        llm_config=CloudLLMConfig(
            api_key_env="DESKTOP_LLM_KEY",
            credential_target="VirtualCompanion/LLM",
        )
    )
    monkeypatch.setenv("DESKTOP_LLM_KEY", "secret")
    host = DesktopHost(FakeApp(), config)

    with pytest.raises(ControlError) as raised:
        await host._handle_request(
            "credential.set",
            {"kind": "llm", "value": "new-secret", "overwrite": True},
        )

    assert raised.value.code == "environment_override"


@pytest.mark.asyncio
async def test_empty_credential_value_is_rejected(monkeypatch) -> None:
    write_credential = AsyncMock()
    monkeypatch.setattr("companion.desktop.host.write_windows_credential", write_credential)
    host = DesktopHost(
        FakeApp(),
        RuntimeConfig(
            llm_config=CloudLLMConfig(
                api_key_env="",
                credential_target="VirtualCompanion/LLM",
            )
        ),
    )

    with pytest.raises(ControlError) as raised:
        await host._handle_request(
            "credential.set",
            {"kind": "llm", "value": "   ", "overwrite": False},
        )

    assert raised.value.code == "invalid_params"
    write_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_credential_write_schedules_readiness_without_echoing_value(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def write_credential(target: str, value: str, *, overwrite: bool) -> None:
        captured.update(target=target, value=value, overwrite=overwrite)

    monkeypatch.setattr("companion.desktop.host.write_windows_credential", write_credential)
    host = DesktopHost(
        FakeApp(),
        RuntimeConfig(
            llm_config=CloudLLMConfig(
                api_key_env="",
                credential_target="VirtualCompanion/LLM",
            )
        ),
    )
    readiness_started = asyncio.Event()

    async def readiness() -> None:
        readiness_started.set()

    host._run_readiness = readiness  # type: ignore[method-assign]
    secret = "desktop-secret-that-must-not-echo"

    result = await host._handle_request(
        "credential.set",
        {"kind": "llm", "value": secret, "overwrite": True},
    )
    await asyncio.wait_for(readiness_started.wait(), timeout=1)

    assert captured == {
        "target": "VirtualCompanion/LLM",
        "value": secret,
        "overwrite": True,
    }
    assert result == {"kind": "llm", "status": "windows_credential"}
    assert secret not in str(result)


@pytest.mark.asyncio
async def test_credential_write_succeeds_during_active_turn_without_busy_error(
    monkeypatch,
) -> None:
    writes: list[tuple[str, str, bool]] = []

    def write_credential(target: str, value: str, *, overwrite: bool) -> None:
        writes.append((target, value, overwrite))

    monkeypatch.setattr("companion.desktop.host.write_windows_credential", write_credential)
    monkeypatch.setattr(
        "companion.desktop.host.read_windows_credential",
        lambda _target: "",
    )
    host = DesktopHost(
        FakeApp(),
        RuntimeConfig(
            llm_config=CloudLLMConfig(
                api_key_env="",
                credential_target="VirtualCompanion/LLM",
            )
        ),
    )
    host._active_turn_id = "turn-active"
    host._server.publish = AsyncMock(return_value=True)

    result = await host._handle_request(
        "credential.set",
        {"kind": "llm", "value": "desktop-secret", "overwrite": False},
    )
    await asyncio.sleep(0)

    assert result == {"kind": "llm", "status": "windows_credential"}
    assert writes == [("VirtualCompanion/LLM", "desktop-secret", False)]
    assert host._readiness_task is None
    assert host._server.publish.await_count == 1
    assert host._server.publish.await_args.args[0] == "runtime.snapshot"


@pytest.mark.asyncio
async def test_missing_provider_credentials_still_launch_desktop_interface(
    monkeypatch,
) -> None:
    app = FakeApp()
    app.start_desktop_runtime = AsyncMock(return_value=True)
    app.retry_desktop_readiness = AsyncMock(
        return_value={
            "llm": "unavailable",
            "tts": "unavailable",
            "asr": "healthy",
            "memory": "healthy",
            "avatar": "healthy",
            "runtime": "unavailable",
        }
    )
    host = DesktopHost(
        app,
        RuntimeConfig(avatar_stage_launch_config=_desktop_launch_config()),
    )
    host._ensure_avatar_credential = lambda: None  # type: ignore[method-assign]
    host._server.start = AsyncMock()
    host._server.stop = AsyncMock()
    host._server.wait_until_connected = AsyncMock()
    host._server.publish = AsyncMock(return_value=True)
    monkeypatch.setattr(
        type(host._server),
        "url",
        property(lambda _server: "ws://127.0.0.1:49152"),
    )

    async def quit_after_readiness() -> None:
        host._quit.set()

    original_readiness = host._run_readiness

    async def run_readiness() -> None:
        await original_readiness()
        await quit_after_readiness()

    host._run_readiness = run_readiness  # type: ignore[method-assign]

    assert await host.run() is True
    app.start_desktop_runtime.assert_awaited_once()
    assert host._phase == "stopping"
    snapshots = [call.args[1] for call in host._server.publish.await_args_list]
    assert any(snapshot.get("phase") == "setup_required" for snapshot in snapshots)


@pytest.mark.asyncio
async def test_retry_during_initial_readiness_does_not_start_second_readiness(
    monkeypatch,
) -> None:
    app = FakeApp()
    app.start_desktop_runtime = AsyncMock(return_value=True)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_readiness() -> dict[str, str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {
            "llm": "healthy",
            "runtime": "healthy",
            "asr": "healthy",
            "tts": "healthy",
            "memory": "healthy",
            "avatar": "healthy",
        }

    app.retry_desktop_readiness = slow_readiness
    host = DesktopHost(
        app,
        RuntimeConfig(avatar_stage_launch_config=_desktop_launch_config()),
    )
    host._ensure_avatar_credential = lambda: None  # type: ignore[method-assign]
    host._server.start = AsyncMock()
    host._server.stop = AsyncMock()
    host._server.wait_until_connected = AsyncMock()
    host._server.publish = AsyncMock(return_value=True)
    monkeypatch.setattr(
        type(host._server),
        "url",
        property(lambda _server: "ws://127.0.0.1:49152"),
    )

    run_task = asyncio.create_task(host.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert host._readiness_task is not None
    assert not host._readiness_task.done()

    assert await host._handle_request("runtime.retry", {}) == {"accepted": True}
    assert calls == 1

    release.set()
    host._quit.set()
    assert await asyncio.wait_for(run_task, timeout=2) is True
    assert calls == 1


@pytest.mark.asyncio
async def test_voice_turn_events_update_host_active_turn_and_final_user_text() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)
    started = ConversationTurnStartedEvent(
        turn_id="turn-voice",
        session_id="session-current",
        turn_sequence=1,
        modality="voice",
    )

    await host._on_domain_event(started)

    assert host._active_turn_id == "turn-voice"
    published_started = host._server.publish.await_args_list[0].args[1]
    assert published_started["user_text"] == ""

    completed = ConversationTurnCompletedEvent(
        turn_id="turn-voice",
        session_id="session-current",
        turn_sequence=1,
        user_text="语音识别后的文本",
        companion_text="实际播放的回复",
        companion_full_text="不应进入桌面协议",
        total_latency_ms=12,
    )

    await host._on_domain_event(completed)

    assert host._active_turn_id == ""
    completed_payload = next(
        call.args[1]
        for call in host._server.publish.await_args_list
        if call.args[0] == "conversation.turn.completed"
    )
    assert completed_payload["user_text"] == "语音识别后的文本"
    assert "companion_full_text" not in completed_payload


@pytest.mark.asyncio
async def test_interruption_notice_keeps_host_busy_until_completed_terminal() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)
    host._active_turn_id = "turn-interrupted"

    await host._on_domain_event(
        ConversationTurnInterruptedEvent(
            turn_id="turn-interrupted",
            interrupted_at_audio_ms=25,
            new_turn_id="pending",
        )
    )
    assert host._active_turn_id == "turn-interrupted"

    await host._on_domain_event(
        ConversationTurnCompletedEvent(
            turn_id="turn-interrupted",
            session_id="session-current",
            turn_sequence=1,
            user_text="hello",
            companion_text="heard text",
            was_interrupted=True,
            total_latency_ms=12,
        )
    )
    assert host._active_turn_id == ""


@pytest.mark.asyncio
async def test_interruption_without_playback_clears_host_on_failed_terminal() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)
    host._active_turn_id = "turn-no-audio"

    await host._on_domain_event(
        ConversationTurnInterruptedEvent(
            turn_id="turn-no-audio",
            interrupted_at_audio_ms=0,
            new_turn_id="pending",
        )
    )
    assert host._active_turn_id == "turn-no-audio"

    await host._on_domain_event(
        ConversationTurnFailedEvent(
            turn_id="turn-no-audio",
            session_id="session-current",
            turn_sequence=1,
            stage="cancellation",
            error_type="interrupted_before_playback",
            retryable=True,
            elapsed_ms=12,
        )
    )
    assert host._active_turn_id == ""


@pytest.mark.asyncio
async def test_authenticated_control_disconnect_stops_voice_and_exits_host() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._voice.stop = AsyncMock()

    await host._on_disconnected()

    host._voice.stop.assert_awaited_once()
    assert host._quit.is_set()


@pytest.mark.asyncio
async def test_authenticated_disconnect_exits_host_when_voice_stop_fails() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._voice.stop = AsyncMock(side_effect=OSError("audio release failed"))

    with pytest.raises(OSError, match="audio release failed"):
        await host._on_disconnected()

    host._voice.stop.assert_awaited_once()
    assert host._quit.is_set()


@pytest.mark.asyncio
async def test_authenticated_disconnect_unblocks_run_and_executes_cleanup(monkeypatch) -> None:
    app = FakeApp()
    app.start_desktop_runtime = AsyncMock(return_value=True)
    app.retry_desktop_readiness = AsyncMock(
        return_value={
            "llm": "healthy",
            "tts": "healthy",
            "asr": "healthy",
            "memory": "healthy",
            "avatar": "healthy",
            "runtime": "healthy",
        }
    )
    host = DesktopHost(
        app,
        RuntimeConfig(avatar_stage_launch_config=_desktop_launch_config()),
    )
    host._ensure_avatar_credential = lambda: None  # type: ignore[method-assign]
    host._server.start = AsyncMock()
    host._server.stop = AsyncMock()
    host._server.wait_until_connected = AsyncMock()
    host._server.publish = AsyncMock(return_value=True)
    host._voice.stop = AsyncMock()
    monkeypatch.setattr(
        type(host._server),
        "url",
        property(lambda _server: "ws://127.0.0.1:49152"),
    )

    original_readiness = host._run_readiness

    async def readiness_then_disconnect() -> None:
        await original_readiness()
        await host._on_disconnected()

    host._run_readiness = readiness_then_disconnect  # type: ignore[method-assign]

    assert await asyncio.wait_for(host.run(), timeout=1) is True
    assert host._phase == "stopping"
    host._server.stop.assert_awaited_once()
    assert host._voice.stop.await_count >= 2


@pytest.mark.asyncio
async def test_run_stops_control_server_when_voice_cleanup_fails(monkeypatch) -> None:
    app = FakeApp()
    app.start_desktop_runtime = AsyncMock(return_value=True)
    app.retry_desktop_readiness = AsyncMock(
        return_value={
            "llm": "healthy",
            "tts": "healthy",
            "asr": "healthy",
            "memory": "healthy",
            "avatar": "healthy",
            "runtime": "healthy",
        }
    )
    host = DesktopHost(
        app,
        RuntimeConfig(avatar_stage_launch_config=_desktop_launch_config()),
    )
    host._ensure_avatar_credential = lambda: None  # type: ignore[method-assign]
    host._server.start = AsyncMock()
    host._server.stop = AsyncMock()
    host._server.wait_until_connected = AsyncMock()
    host._server.publish = AsyncMock(return_value=True)
    host._voice.stop = AsyncMock(side_effect=OSError("audio release failed"))
    monkeypatch.setattr(
        type(host._server),
        "url",
        property(lambda _server: "ws://127.0.0.1:49152"),
    )

    async def readiness_then_quit() -> None:
        host._quit.set()

    host._run_readiness = readiness_then_quit  # type: ignore[method-assign]

    assert await host.run() is True

    host._server.stop.assert_awaited_once()
    assert app.voice_pipeline.assistant_delta_callback is None


@pytest.mark.asyncio
async def test_unexpected_turn_task_failure_does_not_leave_host_busy() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)
    host._active_turn_id = "turn-crashed"
    failed_task = asyncio.get_running_loop().create_future()
    failed_task.set_exception(RuntimeError("provider crashed"))

    host._turn_done("turn-crashed", failed_task)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert host._active_turn_id == ""
    assert host._turn_task is None


@pytest.mark.asyncio
async def test_unexpected_turn_task_failure_publishes_single_failure_terminal() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)
    host._active_turn_id = "turn-crashed"
    failed_task = asyncio.get_running_loop().create_future()
    failed_task.set_exception(RuntimeError("provider crashed"))

    host._turn_done("turn-crashed", failed_task)
    for _ in range(3):
        await asyncio.sleep(0)

    failures = [
        call.args[1]
        for call in host._server.publish.await_args_list
        if call.args[0] == "conversation.turn.failed"
    ]
    assert failures == [
        {
            "turn_id": "turn-crashed",
            "stage": "generation",
            "error_type": "runtime_error",
            "retryable": True,
        }
    ]
    assert host._active_turn_id == ""


@pytest.mark.asyncio
async def test_host_cancel_request_publishes_cancelled_failure_and_clears_busy() -> None:
    app = FakeApp()
    pipeline = app.voice_pipeline
    pipeline.process_text_input = AsyncMock()  # type: ignore[attr-defined]
    pipeline.cancel = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    host = DesktopHost(app, RuntimeConfig())
    host._provider_status["runtime"] = "healthy"
    host._server.publish = AsyncMock(return_value=True)

    result = await host._handle_request(
        "conversation.send", {"text": "hello", "speak": False}
    )
    turn_id = str(result["turn_id"])
    assert host._active_turn_id == turn_id

    assert await host._handle_request("conversation.cancel", {"turn_id": turn_id}) == {
        "cancelled": True
    }
    pipeline.cancel.assert_awaited_once_with(turn_id)  # type: ignore[attr-defined]

    await host._on_domain_event(
        ConversationTurnFailedEvent(
            turn_id=turn_id,
            session_id="session-current",
            turn_sequence=1,
            stage="cancellation",
            error_type="cancelled",
            retryable=True,
            elapsed_ms=12,
        )
    )
    assert host._active_turn_id == ""
    failure_payload = next(
        call.args[1]
        for call in host._server.publish.await_args_list
        if call.args[0] == "conversation.turn.failed"
    )
    assert failure_payload["stage"] == "cancellation"
    assert failure_payload["error_type"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_is_rejected_during_active_turn() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._active_turn_id = "turn-active"

    with pytest.raises(ControlError) as raised:
        await host._handle_request("runtime.retry", {})

    assert raised.value.code == "runtime_busy"


@pytest.mark.asyncio
async def test_voice_preload_refreshes_provider_snapshot() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._provider_status = {
        "llm": "healthy",
        "runtime": "healthy",
        "asr": "degraded",
        "tts": "healthy",
    }
    host._voice.start = AsyncMock(return_value=True)
    host._server.publish = AsyncMock(return_value=True)

    result = await host._start_voice()

    assert result == {"started": True}
    assert host._provider_status["asr"] == "healthy"
    assert host._provider_status["tts"] == "healthy"
    assert host._phase == "ready"
    assert host._server.publish.await_args.args[1]["providers"]["asr"] == "healthy"


@pytest.mark.asyncio
async def test_voice_start_rejected_until_language_provider_ready() -> None:
    host = DesktopHost(FakeApp(), RuntimeConfig())
    host._provider_status = {"runtime": "unavailable", "asr": "healthy", "tts": "healthy"}
    host._voice.start = AsyncMock(return_value=True)

    with pytest.raises(ControlError) as raised:
        await host._handle_request("voice.start", {})

    assert raised.value.code == "setup_required"
    host._voice.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_failure_publishes_sanitized_retryable_snapshot() -> None:
    app = FakeApp()
    app.retry_desktop_readiness = AsyncMock(
        side_effect=RuntimeError("SECRET C:/provider/private-response")
    )
    host = DesktopHost(app, RuntimeConfig())
    host._server.publish = AsyncMock(return_value=True)

    await host._run_readiness()

    snapshot = host._server.publish.await_args_list[-1].args[1]
    serialized = str(snapshot)
    assert snapshot["phase"] == "error"
    assert snapshot["error"] == {
        "code": "readiness_failed",
        "message": "Provider readiness could not be completed.",
        "retryable": True,
    }
    assert "SECRET" not in serialized
    assert "private-response" not in serialized


def test_child_environment_injects_control_boundary_without_cloud_secrets() -> None:
    environment = _build_child_environment(
        {
            "PATH": "C:/Windows",
            "ANTHROPIC_API_KEY": "secret",
            "COMPANION_CONTROL_URL": "attacker-value",
        },
        "avatar-token",
        control_url="ws://127.0.0.1:49152",
        control_token="control-token",
    )

    assert environment["COMPANION_AVATAR_TOKEN"] == "avatar-token"
    assert environment["COMPANION_CONTROL_URL"] == "ws://127.0.0.1:49152"
    assert environment["COMPANION_CONTROL_TOKEN"] == "control-token"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "attacker-value" not in environment.values()

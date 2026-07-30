"""Regression tests for the production avatar stage gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
from argparse import Namespace

import pytest

from companion.__main__ import async_main
from companion.avatar_acceptance import run_avatar_acceptance
from companion.providers.avatar import AvatarModel
from companion.providers.base import ProviderHealth
from companion.providers.implementations.websocket_avatar import (
    AvatarBridgeError,
    AvatarStageInspection,
    WebSocketAvatarConfig,
    WebSocketAvatarProvider,
)


class AcceptanceAvatarProvider:
    def __init__(
        self,
        _config=None,
        *,
        advance_frame: bool = True,
        preapply_frame_only: bool = False,
        health_sequence: list[ProviderHealth] | None = None,
    ) -> None:
        self.advance_frame = advance_frame
        self.preapply_frame_only = preapply_frame_only
        self.health_sequence = list(health_sequence or [ProviderHealth.HEALTHY])
        self.health_checks = 0
        self.loaded = False
        self.state_sequence = 0
        self.frame_sequence = 4
        self.expression_sequence = 0
        self.gesture_sequence = 0
        self.proactive_sequence = 0
        self.expression_id = "neutral"
        self.valence = 0.0
        self.arousal = 0.5
        self.proactive_level = 0
        self.gesture_id = ""
        self.state_gesture_ids: list[str | None] = []

    async def health_check(self) -> ProviderHealth:
        self.health_checks += 1
        if len(self.health_sequence) > 1:
            return self.health_sequence.pop(0)
        return self.health_sequence[0]

    async def list_available_models(self) -> list[AvatarModel]:
        return [AvatarModel("kurisu", "Kurisu", "live2d", "kurisu.model3.json")]

    async def validate_model(self, model_id: str) -> list[str]:
        return [] if model_id == "kurisu" else ["missing"]

    async def load_model(self, model_id: str) -> bool:
        self.loaded = model_id == "kurisu"
        return self.loaded

    async def inspect_stage(self) -> AvatarStageInspection:
        if self.loaded and self.advance_frame:
            self.frame_sequence += 1
        return AvatarStageInspection(
            renderer="live2d",
            model_id="kurisu" if self.loaded else "",
            model_loaded=self.loaded,
            visible=self.loaded,
            state_sequence=self.state_sequence,
            rendered_state_sequence=self.state_sequence,
            frame_sequence=self.frame_sequence,
            expression_sequence=self.expression_sequence,
            gesture_sequence=self.gesture_sequence,
            proactive_sequence=self.proactive_sequence,
            expression_id=self.expression_id,
            valence=self.valence,
            arousal=self.arousal,
            proactive_level=self.proactive_level,
            last_gesture_id=self.gesture_id,
        )

    async def update_state(self, state) -> None:
        self.state_sequence += 1
        self.state_gesture_ids.append(state.pose.gesture_id)
        if self.preapply_frame_only:
            self.frame_sequence += 1
        self.expression_id = state.expression.expression_id
        self.valence = state.valence
        self.arousal = state.arousal

    async def set_proactive_level(self, level: int) -> None:
        self.proactive_level = level
        self.proactive_sequence += 1

    async def trigger_expression(
        self, expression_id: str, intensity: float = 0.5, duration_ms: int = 2000
    ) -> None:
        del intensity, duration_ms
        self.expression_id = expression_id
        self.expression_sequence += 1

    async def trigger_gesture(self, gesture_id: str, intensity: float = 0.5) -> None:
        del intensity
        self.gesture_id = gesture_id
        self.gesture_sequence += 1

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_avatar_acceptance_waits_for_renderer_health() -> None:
    provider = AcceptanceAvatarProvider(
        health_sequence=[ProviderHealth.UNHEALTHY, ProviderHealth.HEALTHY]
    )

    report = await run_avatar_acceptance(
        provider, model_id="kurisu", apply_timeout_seconds=0.2
    )

    assert report.exit_code == 0
    assert report.checks[0].code == "avatar.bridge_health"
    assert provider.health_checks == 2


@pytest.mark.asyncio
async def test_avatar_acceptance_health_wait_is_bounded() -> None:
    provider = AcceptanceAvatarProvider(
        health_sequence=[ProviderHealth.UNHEALTHY]
    )

    report = await run_avatar_acceptance(
        provider, model_id="kurisu", apply_timeout_seconds=0.01
    )

    assert report.exit_code == 1
    assert report.checks[0].code == "avatar.bridge_health"
    assert provider.health_checks >= 1


@pytest.mark.asyncio
async def test_avatar_acceptance_requires_presented_frame() -> None:
    provider = AcceptanceAvatarProvider(advance_frame=False)

    report = await run_avatar_acceptance(
        provider, model_id="kurisu", apply_timeout_seconds=0.01
    )

    assert report.exit_code == 1
    assert report.checks[-2].code == "avatar.state_rendered"
    assert report.checks[-2].passed
    assert report.checks[-1].code == "avatar.frame_presented"
    assert not report.checks[-1].passed


@pytest.mark.asyncio
async def test_avatar_acceptance_requires_frame_after_state_application() -> None:
    provider = AcceptanceAvatarProvider(
        advance_frame=False, preapply_frame_only=True
    )

    report = await run_avatar_acceptance(
        provider, model_id="kurisu", apply_timeout_seconds=0.01
    )

    assert report.checks[-2].passed
    assert not report.checks[-1].passed
    assert provider.state_gesture_ids == [None]
    assert provider.gesture_sequence == 1


@pytest.mark.asyncio
async def test_avatar_acceptance_json_fails_when_avatar_is_disabled(capsys) -> None:
    args = Namespace(
        config=None,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=True,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    assert await async_main(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["code"] == "avatar.config"
    assert payload["passed"] is False


@pytest.mark.asyncio
async def test_avatar_acceptance_cli_runs_without_llm_readiness(
    tmp_path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "avatar.yaml"
    config_path.write_text(
        """identity:
  avatar_model_id: kurisu
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: TEST_AVATAR_ACCEPTANCE_TOKEN
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_AVATAR_ACCEPTANCE_TOKEN", "test-token")
    monkeypatch.setattr(
        "companion.__main__.WebSocketAvatarProvider", AcceptanceAvatarProvider
    )
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=True,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    assert await async_main(args) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["passed"] is True
    assert payload["schema_version"] == 1
    assert payload["app_version"]
    assert payload["generated_at"]
    assert payload["checks"][-1]["code"] == "avatar.frame_presented"
    assert "Observe the stage now" in captured.err


@pytest.mark.asyncio
async def test_managed_avatar_acceptance_launches_and_cleans_stage(
    tmp_path, monkeypatch, capsys
) -> None:
    executable = tmp_path / "airi.exe"
    content = b"MZmanaged-airi"
    executable.write_bytes(content)
    app_asar = tmp_path / "resources" / "app.asar"
    app_asar.parent.mkdir()
    app_asar_content = b"managed-airi-application"
    app_asar.write_bytes(app_asar_content)
    godot = tmp_path / "resources" / "godot-stage" / "godot-stage.exe"
    godot.parent.mkdir()
    godot_content = b"MZmanaged-godot-sidecar"
    godot.write_bytes(godot_content)
    model = tmp_path / "nemesia.vrm"
    model_content = b"managed-vrm"
    model.write_bytes(model_content)
    config_path = tmp_path / "avatar.yaml"
    config_path.write_text(
        f"""identity:
  avatar_model_id: kurisu
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: {executable.as_posix()}
      expected_sha256: {hashlib.sha256(content).hexdigest()}
      expected_app_asar_sha256: {hashlib.sha256(app_asar_content).hexdigest()}
      expected_godot_sha256: {hashlib.sha256(godot_content).hexdigest()}
      model_path: {model.as_posix()}
      expected_model_sha256: {hashlib.sha256(model_content).hexdigest()}
      model_id: kurisu
      model_name: Nemesia pajamas
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "test-token")
    monkeypatch.setattr(
        "companion.__main__.WebSocketAvatarProvider", AcceptanceAvatarProvider
    )
    calls: list[str] = []

    class Stage:
        def __init__(self, _config) -> None:
            calls.append("init")

        async def start(self, token: str) -> None:
            assert token == "test-token"
            calls.append("start")

        async def wait_until_bridge_ready(self) -> None:
            calls.append("ready")

        async def shutdown(self) -> None:
            calls.append("shutdown")

    monkeypatch.setattr("companion.__main__.AvatarStageSupervisor", Stage)
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=True,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    assert await async_main(args) == 0
    assert calls == ["init", "start", "ready", "shutdown"]
    assert json.loads(capsys.readouterr().out)["passed"] is True


@pytest.mark.asyncio
async def test_managed_avatar_acceptance_cancellation_still_cleans_stage(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "airi.exe"
    content = b"MZmanaged-airi"
    executable.write_bytes(content)
    app_asar = tmp_path / "resources" / "app.asar"
    app_asar.parent.mkdir()
    app_asar_content = b"managed-airi-application"
    app_asar.write_bytes(app_asar_content)
    godot = tmp_path / "resources" / "godot-stage" / "godot-stage.exe"
    godot.parent.mkdir()
    godot_content = b"MZmanaged-godot-sidecar"
    godot.write_bytes(godot_content)
    model = tmp_path / "nemesia.vrm"
    model_content = b"managed-vrm"
    model.write_bytes(model_content)
    config_path = tmp_path / "avatar.yaml"
    config_path.write_text(
        f"""identity:
  avatar_model_id: kurisu
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: {executable.as_posix()}
      expected_sha256: {hashlib.sha256(content).hexdigest()}
      expected_app_asar_sha256: {hashlib.sha256(app_asar_content).hexdigest()}
      expected_godot_sha256: {hashlib.sha256(godot_content).hexdigest()}
      model_path: {model.as_posix()}
      expected_model_sha256: {hashlib.sha256(model_content).hexdigest()}
      model_id: kurisu
      model_name: Nemesia pajamas
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPANION_AVATAR_TOKEN", "test-token")
    calls: list[str] = []

    class Provider(AcceptanceAvatarProvider):
        async def shutdown(self) -> None:
            calls.append("provider.shutdown")

    class Stage:
        def __init__(self, _config) -> None:
            return None

        async def start(self, _token: str) -> None:
            calls.append("stage.start")

        async def wait_until_bridge_ready(self) -> None:
            calls.append("stage.ready")

        async def shutdown(self) -> None:
            calls.append("stage.shutdown")

    async def cancel_acceptance(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("companion.__main__.WebSocketAvatarProvider", Provider)
    monkeypatch.setattr("companion.__main__.AvatarStageSupervisor", Stage)
    monkeypatch.setattr("companion.__main__.run_avatar_acceptance", cancel_acceptance)
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=True,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await async_main(args)

    assert calls == [
        "stage.start",
        "stage.ready",
        "provider.shutdown",
        "stage.shutdown",
    ]


@pytest.mark.asyncio
async def test_stage_inspection_rejects_boolean_sequence() -> None:
    provider = WebSocketAvatarProvider(WebSocketAvatarConfig())

    async def malformed(_method: str, _params: dict) -> dict:
        return {
            "renderer": "live2d",
            "model_id": "kurisu",
            "model_loaded": True,
            "visible": True,
            "state_sequence": True,
            "rendered_state_sequence": 1,
            "frame_sequence": 1,
            "expression_sequence": 0,
            "gesture_sequence": 0,
            "proactive_sequence": 0,
            "expression_id": "neutral",
            "valence": 0.0,
            "arousal": 0.5,
            "proactive_level": 0,
            "last_gesture_id": "",
        }

    provider._request = malformed  # type: ignore[method-assign]

    with pytest.raises(AvatarBridgeError, match="state_sequence"):
        await provider.inspect_stage()

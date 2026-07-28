"""Regression tests for the production avatar stage gate."""

from __future__ import annotations

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
    ) -> None:
        self.advance_frame = advance_frame
        self.preapply_frame_only = preapply_frame_only
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

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

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
    url: ws://127.0.0.1:6121/ws
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
    assert payload["checks"][-1]["code"] == "avatar.frame_presented"
    assert "Observe the stage now" in captured.err


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

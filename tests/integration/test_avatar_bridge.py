"""End-to-end contract tests for the external avatar WebSocket boundary."""

from __future__ import annotations

import json

import pytest
from websockets.asyncio.server import ServerConnection, serve

from companion.avatar_acceptance import run_avatar_acceptance
from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.providers.avatar import AvatarState, FacialExpression
from companion.providers.base import ProviderHealth
from companion.providers.implementations.websocket_avatar import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    AvatarBridgeError,
    WebSocketAvatarConfig,
    WebSocketAvatarProvider,
)
from tests.test_providers import MockLLMProvider


class FakeAvatarStage:
    def __init__(self) -> None:
        self.connection_count = 0
        self.requests: list[dict] = []
        self.states: list[dict] = []
        self.proactive_levels: list[int] = []
        self.loaded_model_id = ""
        self.state_sequence = 0
        self.rendered_state_sequence = 0
        self.frame_sequence = 0
        self.expression_sequence = 0
        self.gesture_sequence = 0
        self.proactive_sequence = 0
        self.last_expression_id = "neutral"
        self.last_gesture_id = ""

    async def handle(self, connection: ServerConnection) -> None:
        self.connection_count += 1
        authenticated = False
        async for message in connection:
            request = json.loads(message)
            self.requests.append(request)
            assert request["protocol"] == PROTOCOL_NAME
            assert request["version"] == PROTOCOL_VERSION
            method = request["method"]
            params = request["params"]

            if method == "handshake":
                authenticated = params["auth_token"] == "stage-test-token"
                await self._reply(
                    connection,
                    request,
                    ok=authenticated,
                    result={"version": PROTOCOL_VERSION},
                    error="unauthorized",
                )
                continue
            assert authenticated

            if method == "health":
                result = {"status": "healthy"}
            elif method == "model.list":
                result = {
                    "models": [
                        {
                            "model_id": "kurisu",
                            "name": "Lab Member 004",
                            "type": "live2d",
                            "path": "models/kurisu.model3.json",
                            "expressions": ["neutral", "happy"],
                            "validation_errors": [],
                        }
                    ]
                }
            elif method == "model.validate":
                result = {"errors": []}
            elif method == "model.load":
                loaded = params["model_id"] == "kurisu"
                if loaded:
                    self.loaded_model_id = params["model_id"]
                result = {"loaded": loaded}
            elif method == "state.update":
                self.states.append(params["state"])
                self.state_sequence += 1
                self.rendered_state_sequence = self.state_sequence
                self.last_expression_id = params["state"]["expression"]["expression_id"]
                result = {}
            elif method == "proactive.set_level":
                self.proactive_levels.append(params["level"])
                self.proactive_sequence += 1
                result = {}
            elif method == "expression.trigger" and params["expression_id"] == "timeout":
                continue
            elif method == "gesture.trigger" and params["gesture_id"] == "disconnect":
                await connection.close()
                return
            elif method == "expression.trigger":
                self.last_expression_id = params["expression_id"]
                self.expression_sequence += 1
                result = {}
            elif method == "gesture.trigger":
                self.last_gesture_id = params["gesture_id"]
                self.gesture_sequence += 1
                result = {}
            elif method == "stage.inspect":
                if self.loaded_model_id:
                    self.frame_sequence += 1
                latest_state = self.states[-1] if self.states else {}
                result = {
                    "renderer": "live2d",
                    "model_id": self.loaded_model_id,
                    "model_loaded": bool(self.loaded_model_id),
                    "visible": bool(self.loaded_model_id),
                    "state_sequence": self.state_sequence,
                    "rendered_state_sequence": self.rendered_state_sequence,
                    "frame_sequence": self.frame_sequence,
                    "expression_sequence": self.expression_sequence,
                    "gesture_sequence": self.gesture_sequence,
                    "proactive_sequence": self.proactive_sequence,
                    "expression_id": self.last_expression_id,
                    "valence": latest_state.get("valence", 0.0),
                    "arousal": latest_state.get("arousal", 0.5),
                    "proactive_level": self.proactive_levels[-1]
                    if self.proactive_levels
                    else 0,
                    "last_gesture_id": self.last_gesture_id,
                }
            else:
                result = {}
            await self._reply(connection, request, ok=True, result=result)

    @staticmethod
    async def _reply(
        connection: ServerConnection,
        request: dict,
        *,
        ok: bool,
        result: dict,
        error: str = "",
    ) -> None:
        response = {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "type": "response",
            "id": request["id"],
            "ok": ok,
        }
        response["result" if ok else "error"] = result if ok else error
        await connection.send(json.dumps(response))


@pytest.mark.asyncio
async def test_avatar_bridge_contract_state_mapping_and_reconnect(monkeypatch) -> None:
    monkeypatch.setenv("TEST_AVATAR_TOKEN", "stage-test-token")
    stage = FakeAvatarStage()
    async with serve(stage.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        provider = WebSocketAvatarProvider(
            WebSocketAvatarConfig(
                url=f"ws://127.0.0.1:{port}",
                auth_token_env="TEST_AVATAR_TOKEN",
                request_timeout_seconds=0.05,
            )
        )

        assert await provider.health_check() == ProviderHealth.HEALTHY
        models = await provider.list_available_models()
        assert models[0].model_id == "kurisu"
        assert await provider.validate_model("kurisu") == []
        assert await provider.load_model("kurisu")

        await provider.update_state(
            AvatarState(expression=FacialExpression("happy", intensity=0.8), valence=0.7)
        )
        assert stage.states[-1]["expression"]["expression_id"] == "happy"
        assert stage.states[-1]["valence"] == 0.7

        with pytest.raises(AvatarBridgeError, match="timed out"):
            await provider.trigger_expression("timeout")
        with pytest.raises(AvatarBridgeError, match="disconnected"):
            await provider.trigger_gesture("disconnect")

        assert await provider.health_check() == ProviderHealth.HEALTHY
        assert stage.connection_count >= 2
        await provider.shutdown()
        assert await provider.health_check() == ProviderHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_orchestrator_pushes_authoritative_affect_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("TEST_AVATAR_TOKEN", "stage-test-token")
    stage = FakeAvatarStage()
    async with serve(stage.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        provider = WebSocketAvatarProvider(
            WebSocketAvatarConfig(
                url=f"ws://127.0.0.1:{port}", auth_token_env="TEST_AVATAR_TOKEN"
            )
        )
        state = StateManager()
        orchestrator = CompanionOrchestrator(
            state,
            EventBus("avatar-test"),
            PolicyGate(),
            llm_provider=MockLLMProvider(),
            avatar_provider=provider,
        )

        assert await orchestrator.startup()
        result = await orchestrator.process_user_input("今天真开心，太棒了，我很喜欢")

        assert result["response_text"] == "mock response"
        assert stage.states[-1]["valence"] == pytest.approx(state.affect.valence)
        assert stage.states[-1]["arousal"] == pytest.approx(state.affect.arousal)
        assert stage.states[-1]["expression"]["expression_id"] != ""
        assert stage.proactive_levels
        await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_real_stage_acceptance_proves_rendered_state(monkeypatch) -> None:
    monkeypatch.setenv("TEST_AVATAR_TOKEN", "stage-test-token")
    stage = FakeAvatarStage()
    async with serve(stage.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        provider = WebSocketAvatarProvider(
            WebSocketAvatarConfig(
                url=f"ws://127.0.0.1:{port}", auth_token_env="TEST_AVATAR_TOKEN"
            )
        )
        try:
            report = await run_avatar_acceptance(provider, model_id="kurisu")
        finally:
            await provider.shutdown()

    assert report.exit_code == 0
    assert [check.code for check in report.checks] == [
        "avatar.bridge_health",
        "avatar.model_available",
        "avatar.model_loaded",
        "avatar.renderer_ready",
        "avatar.state_rendered",
        "avatar.frame_presented",
    ]

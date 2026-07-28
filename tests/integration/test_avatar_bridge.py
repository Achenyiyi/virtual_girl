"""End-to-end contract tests for the external avatar WebSocket boundary."""

from __future__ import annotations

import json

import pytest
from websockets.asyncio.server import ServerConnection, serve

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
                result = {"loaded": params["model_id"] == "kurisu"}
            elif method == "state.update":
                self.states.append(params["state"])
                result = {}
            elif method == "proactive.set_level":
                self.proactive_levels.append(params["level"])
                result = {}
            elif method == "expression.trigger" and params["expression_id"] == "timeout":
                continue
            elif method == "gesture.trigger" and params["gesture_id"] == "disconnect":
                await connection.close()
                return
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

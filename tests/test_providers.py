"""Tests for provider interfaces and contract verification.

Phase 0 acceptance criteria:
- All provider interfaces are well-defined
- Mock providers can be created and pass health checks
- Provider info carries required metadata
"""

from __future__ import annotations

import pytest

from companion.providers.action import ActionProvider, ActionRequest, ActionResult, SandboxStatus
from companion.providers.asr import ASRProvider, ASRResult, ASRStreamRequest
from companion.providers.avatar import AvatarProvider, AvatarState
from companion.providers.base import ProviderCapability, ProviderHealth, ProviderInfo
from companion.providers.memory import MemoryProvider, SemanticFact
from companion.providers.model import LLMProvider, LLMRequest, LLMResponse
from companion.providers.tts import TTSChunk, TTSProvider, TTSRequest

# ── Mock Providers ──────────────────────────────────────────────────────


class MockLLMProvider(LLMProvider):
    def __init__(self):
        self._info = ProviderInfo(
            name="mock-llm",
            version="0.1.0",
            capabilities=[ProviderCapability.CLOUD, ProviderCapability.STREAMING],
        )
        self._healthy = True
        self._cancelled: set[str] = set()

    def provider_info(self) -> ProviderInfo:
        return self._info

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY if self._healthy else ProviderHealth.UNHEALTHY

    async def shutdown(self) -> None:
        self._healthy = False

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="mock response", turn_id=request.turn_id, model_id="mock", model_provider="cloud"
        )

    async def generate_stream(self, request: LLMRequest):
        yield type(
            "Chunk",
            (),
            {
                "text": "mock",
                "turn_id": request.turn_id,
                "is_first": True,
                "is_final": True,
                "token_index": 0,
            },
        )()

    async def cancel(self, turn_id: str) -> bool:
        self._cancelled.add(turn_id)
        return True


class MockTTSProvider(TTSProvider):
    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="mock-tts",
            version="0.1.0",
            capabilities=[ProviderCapability.STREAMING, ProviderCapability.EMOTION_AWARE],
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def shutdown(self) -> None:
        pass

    async def synthesize(self, request: TTSRequest) -> TTSChunk:
        return TTSChunk(
            audio_bytes=b"mock_audio", turn_id=request.turn_id, is_first=True, is_final=True
        )

    async def synthesize_stream(self, request: TTSRequest):
        yield TTSChunk(audio_bytes=b"mock", turn_id=request.turn_id, is_first=True, is_final=True)

    async def cancel(self, turn_id: str) -> bool:
        return True

    async def list_voices(self):
        return []


class MockASRProvider(ASRProvider):
    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="mock-asr", version="0.1.0", capabilities=[ProviderCapability.REAL_TIME]
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def shutdown(self) -> None:
        pass

    async def stream_recognize(self, request: ASRStreamRequest):
        yield ASRResult(text="mock transcription", turn_id=request.turn_id, is_final=True)

    async def push_audio(self, turn_id: str, audio_bytes: bytes) -> None:
        pass

    async def end_audio(self, turn_id: str) -> None:
        pass

    async def cancel(self, turn_id: str) -> bool:
        return True

    async def transcribe_batch(self, request):
        from companion.providers.asr import ASRBatchResult

        return ASRBatchResult(text="mock")


class MockMemoryProvider(MemoryProvider):
    def __init__(self):
        self._events: list[dict] = []
        self._facts: dict[str, SemanticFact] = {}

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="mock-memory", version="0.1.0", capabilities=[ProviderCapability.OFFLINE]
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def shutdown(self) -> None:
        pass

    async def append_event(self, event_data: dict) -> str:
        eid = event_data.get("event_id", "evt_mock")
        self._events.append(event_data)
        return eid

    async def query_events(self, query):
        return self._events

    async def get_event(self, event_id: str):
        for e in self._events:
            if e.get("event_id") == event_id:
                return e
        return None

    async def upsert_fact(self, fact: SemanticFact) -> str:
        self._facts[fact.key] = fact
        return fact.fact_id

    async def get_fact(self, key: str):
        return self._facts.get(key)

    async def search_facts(self, query: str, category=None, limit=10):
        return [f for f in self._facts.values() if query.lower() in f.value.lower()]

    async def list_fact_updates(self, key: str):
        return []

    async def create_episode(self, episode):
        return episode.episode_id

    async def search_episodes(self, query: str, limit=10, min_salience=0.0):
        return []

    async def get_episode(self, episode_id: str):
        return None

    async def create_reflection(self, reflection):
        return reflection.reflection_id

    async def get_recent_reflections(self, limit=10):
        return []

    async def forget(self, event_ids, reason="user_request"):
        return len(event_ids)

    async def rebuild_from_log(self):
        return {"event_count": len(self._events), "passed_consistency_check": True}

    async def verify_consistency(self):
        return {"is_consistent": True, "error_count": 0, "error_details": []}


class MockActionProvider(ActionProvider):
    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="mock-action", version="0.1.0", capabilities=[ProviderCapability.OFFLINE]
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def shutdown(self) -> None:
        pass

    async def execute(self, request: ActionRequest) -> ActionResult:
        return ActionResult(action_id=request.action_id, success=True, method_used=request.method)

    async def undo(self, action_id: str) -> ActionResult:
        return ActionResult(action_id=action_id, success=True, method_used="api")

    async def preview(self, request: ActionRequest) -> dict:
        return {"description": f"Would execute {request.action_type}"}

    async def verify_sandbox(self, request: ActionRequest) -> SandboxStatus:
        return SandboxStatus(
            verified=True,
            sandbox_id=f"sandbox_{request.action_id}",
            isolation_level="test",
        )

    async def get_permissions(self):
        return []

    async def update_permissions(self, permissions) -> None:
        pass

    async def get_audit_log(self, limit=100, risk_level=None):
        return []


class MockAvatarProvider(AvatarProvider):
    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="mock-avatar", version="0.1.0", capabilities=[ProviderCapability.OFFLINE]
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    async def shutdown(self) -> None:
        pass

    async def load_model(self, model_id: str) -> bool:
        return True

    async def update_state(self, state: AvatarState) -> None:
        pass

    async def trigger_expression(self, expression_id: str, intensity=0.5, duration_ms=2000) -> None:
        pass

    async def trigger_gesture(self, gesture_id: str, intensity=0.5) -> None:
        pass

    async def set_proactive_level(self, level: int) -> None:
        pass

    async def list_available_models(self):
        return []

    async def validate_model(self, model_id: str):
        return []


# ── Tests ────────────────────────────────────────────────────────────────


class TestProviderContracts:
    """All providers must satisfy their interface contract."""

    @pytest.mark.asyncio
    async def test_llm_provider_contract(self):
        p = MockLLMProvider()
        info = p.provider_info()
        assert info.name == "mock-llm"
        assert await p.health_check() == ProviderHealth.HEALTHY
        response = await p.generate(
            LLMRequest(messages=[{"role": "user", "content": "hi"}], turn_id="t1")
        )
        assert response.text == "mock response"
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_tts_provider_contract(self):
        p = MockTTSProvider()
        assert await p.health_check() == ProviderHealth.HEALTHY
        chunk = await p.synthesize(TTSRequest(text="hello", turn_id="t1"))
        assert chunk.audio_bytes == b"mock_audio"
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_asr_provider_contract(self):
        p = MockASRProvider()
        assert await p.health_check() == ProviderHealth.HEALTHY
        results = []
        async for r in p.stream_recognize(ASRStreamRequest(turn_id="t1")):
            results.append(r)
        assert len(results) == 1
        assert results[0].is_final
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_memory_provider_contract(self):
        p = MockMemoryProvider()
        assert await p.health_check() == ProviderHealth.HEALTHY
        eid = await p.append_event({"event_id": "evt_1", "event_type": "test"})
        assert eid == "evt_1"
        fact = SemanticFact(fact_id="f1", key="test", value="val")
        await p.upsert_fact(fact)
        retrieved = await p.get_fact("test")
        assert retrieved is not None
        assert retrieved.value == "val"

        # Rebuild
        result = await p.rebuild_from_log()
        assert result["passed_consistency_check"]
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_action_provider_contract(self):
        p = MockActionProvider()
        result = await p.execute(
            ActionRequest(
                action_id="a1", action_type="search_web", method="dom", risk_level="reversible_low"
            )
        )
        assert result.success
        await p.shutdown()

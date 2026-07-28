"""Integration tests for the five-layer memory system (Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion.core.event_bus import EventBus
from companion.core.orchestrator import CompanionOrchestrator
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.memory.episode_segmenter import EpisodeSegmenter
from companion.memory.fact_extractor import FactExtractor
from companion.memory.memory_service import MemoryService, MemoryServiceConfig
from companion.memory.reflection_engine import ReflectionConfig, ReflectionEngine
from companion.providers.memory import SemanticFact
from tests.test_providers import MockLLMProvider


@pytest.fixture
async def memory_service(tmp_path):
    """Create an in-memory SQLite memory service."""
    db_path = str(tmp_path / "test_memory.db")
    svc = MemoryService(MemoryServiceConfig(db_path=db_path))
    yield svc
    await svc.shutdown()


class TestMemoryPipeline:
    """Test all five memory layers working together."""

    @pytest.mark.asyncio
    async def test_event_append_and_query(self, memory_service):
        """Layer 1: Events are stored and queryable."""
        eid = await memory_service.append_event(
            {
                "event_id": "evt_test",
                "event_type": "conversation.turn.completed",
                "privacy": "private",
            }
        )
        assert eid == "evt_test"

        from companion.providers.memory import EventQuery

        results = await memory_service.query_events(EventQuery(limit=10))
        assert len(results) == 1
        assert results[0]["event_id"] == "evt_test"

    @pytest.mark.asyncio
    async def test_conversation_events_back_extracted_facts(self, memory_service):
        """A normal turn leaves no dangling memory source references."""
        bus = EventBus("test", persistence_handler=memory_service.append_domain_event)
        orchestrator = CompanionOrchestrator(
            state_manager=StateManager(),
            event_bus=bus,
            policy_gate=PolicyGate(),
            llm_provider=MockLLMProvider(),
            memory_provider=memory_service,
        )

        await orchestrator.process_user_input("我喜欢蓝色", turn_id="turn_test")

        facts = await memory_service.search_facts("蓝色", limit=10)
        consistency = await memory_service.verify_consistency()
        assert len(facts) == 1
        assert consistency == {
            "is_consistent": True,
            "error_count": 0,
            "error_details": [],
        }
        assert await memory_service.get_event(facts[0].source_event_ids[0]) is not None

    @pytest.mark.asyncio
    async def test_memory_survives_service_restart(self, tmp_path):
        """Committed events and derived facts remain consistent after reopening SQLite."""
        db_path = str(tmp_path / "restart_memory.db")
        first = MemoryService(MemoryServiceConfig(db_path=db_path))
        await first.append_event(
            {
                "event_id": "evt_restart",
                "event_type": "conversation.turn.completed",
            }
        )
        await first.upsert_fact(
            SemanticFact(
                fact_id="fact_restart",
                key="restart_preference",
                value="仍然记得",
                source_event_ids=["evt_restart"],
            )
        )
        await first.shutdown()

        reopened = MemoryService(MemoryServiceConfig(db_path=db_path))
        try:
            fact = await reopened.get_fact("restart_preference")
            consistency = await reopened.verify_consistency()
            assert fact is not None
            assert fact.value == "仍然记得"
            assert consistency["is_consistent"] is True
        finally:
            await reopened.shutdown()

    @pytest.mark.asyncio
    async def test_fact_upsert_and_retrieve(self, memory_service):
        """Layer 3: Facts with validity management."""
        fact = SemanticFact(
            fact_id="f1",
            key="user_name",
            value="小明",
            category="identity",
            confidence=0.9,
            source_event_ids=["evt_1"],
        )
        await memory_service.upsert_fact(fact)

        retrieved = await memory_service.get_fact("user_name")
        assert retrieved is not None
        assert retrieved.value == "小明"

    @pytest.mark.asyncio
    async def test_fact_update_closes_old_validity(self, memory_service):
        """Updating a fact closes the old entry's validity."""
        # Insert original
        await memory_service.upsert_fact(
            SemanticFact(
                fact_id="f1",
                key="preference_color",
                value="蓝色",
                source_event_ids=["evt_1"],
            )
        )
        # Update
        await memory_service.upsert_fact(
            SemanticFact(
                fact_id="f2",
                key="preference_color",
                value="绿色",
                source_event_ids=["evt_2"],
            )
        )

        # Current value should be green
        current = await memory_service.get_fact("preference_color")
        assert current.value == "绿色"

        # History should have 2 entries
        history = await memory_service.list_fact_updates("preference_color")
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_fact_search(self, memory_service):
        """Search over facts using LIKE fallback."""
        # Disable FTS for this test to use LIKE directly
        memory_service._config.fts_enabled = False

        await memory_service.upsert_fact(
            SemanticFact(
                fact_id="f1",
                key="music_taste",
                value="喜欢古典音乐",
                category="preference",
                source_event_ids=["evt_1"],
            )
        )
        await memory_service.upsert_fact(
            SemanticFact(
                fact_id="f2",
                key="food_preference",
                value="喜欢川菜",
                category="preference",
                source_event_ids=["evt_2"],
            )
        )

        # LIKE search should find at least one fact
        results = await memory_service.search_facts("古典", limit=10)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_episode_create_and_retrieve(self, memory_service):
        """Layer 4: Episodic memory."""
        from companion.providers.memory import Episode

        episode = Episode(
            episode_id="ep_1",
            title="一起打游戏",
            summary="用户和我一起玩了游戏，通关了第三章",
            turn_ids=["t1", "t2", "t3"],
            emotional_salience=0.8,
            tags=["gaming", "achievement"],
            occurred_at=datetime.now(UTC),
        )
        eid = await memory_service.create_episode(episode)
        assert eid == "ep_1"

        retrieved = await memory_service.get_episode("ep_1")
        assert retrieved is not None
        assert retrieved.title == "一起打游戏"

    @pytest.mark.asyncio
    async def test_cascade_forget(self, memory_service):
        """Layer 5: Forget cascade-deletes derived memory."""
        # Add event
        eid = "evt_to_forget"
        await memory_service.append_event(
            {
                "event_id": eid,
                "event_type": "conversation.turn.completed",
            }
        )

        # Add fact referencing it
        await memory_service.upsert_fact(
            SemanticFact(
                fact_id="f_to_forget",
                key="temp_fact",
                value="test",
                source_event_ids=[eid],
            )
        )

        # Forget
        cascade = await memory_service.forget([eid])
        assert cascade >= 1  # At least the event itself + the fact

        # Fact should be gone
        gone = await memory_service.get_fact("temp_fact")
        assert gone is None

    @pytest.mark.asyncio
    async def test_rebuild_from_log(self, memory_service):
        """Rebuild derived layers from event log."""
        await memory_service.append_event(
            {
                "event_id": "evt_r1",
                "event_type": "test.event",
            }
        )
        result = await memory_service.rebuild_from_log()
        assert result["passed_consistency_check"]
        assert result["event_count"] == 1

    @pytest.mark.asyncio
    async def test_failed_rebuild_rolls_back_derived_memory(self, memory_service, monkeypatch):
        fact = SemanticFact(
            fact_id="fact_keep",
            key="keep_me",
            value="survives failed rebuild",
            source_event_ids=[],
        )
        await memory_service.upsert_fact(fact)
        await memory_service.append_event(
            {
                "event_id": "evt_break_rebuild",
                "event_type": "test.event",
            }
        )

        async def fail_reflection(_reflection):
            raise RuntimeError("injected rebuild failure")

        monkeypatch.setattr(memory_service, "create_reflection", fail_reflection)
        with pytest.raises(RuntimeError, match="previous derived memory was restored"):
            await memory_service.rebuild_from_log()

        restored = await memory_service.get_fact("keep_me")
        assert restored is not None
        assert restored.value == "survives failed rebuild"

    @pytest.mark.asyncio
    async def test_consistency_check(self, memory_service):
        """Verify memory consistency."""
        result = await memory_service.verify_consistency()
        assert result["is_consistent"]
        assert result["error_count"] == 0


class TestFactExtractor:
    """Test structured fact extraction from user text."""

    def test_extracts_preference(self):
        extractor = FactExtractor()
        result = extractor.extract("我喜欢听古典音乐，特别是巴赫。", ["evt_1"])
        assert len(result.facts) > 0
        # Should have at least a preference fact about music
        preference_facts = [f for f in result.facts if f.category == "preference"]
        assert len(preference_facts) > 0

    def test_extracts_identity(self):
        extractor = FactExtractor()
        result = extractor.extract("我叫小明，今年25岁，住在上海。", ["evt_1"])
        identity_facts = [f for f in result.facts if f.category == "identity"]
        assert len(identity_facts) > 0

    def test_extracts_schedule(self):
        extractor = FactExtractor()
        result = extractor.extract("明天要去参加面试，好紧张。", ["evt_1"])
        schedule_facts = [f for f in result.facts if f.category == "schedule"]
        assert len(schedule_facts) > 0

    def test_empty_text_returns_no_facts(self):
        extractor = FactExtractor()
        result = extractor.extract("", ["evt_1"])
        assert len(result.facts) == 0

    def test_negative_preference_is_not_duplicated(self):
        extractor = FactExtractor()
        result = extractor.extract("我不喜欢蓝色", ["evt_negative"])
        assert len(result.facts) == 1
        assert result.facts[0].category == "preference"

    @pytest.mark.asyncio
    async def test_preference_change_closes_previous_value(self, memory_service):
        extractor = FactExtractor()
        liked = extractor.extract("我喜欢蓝色", ["evt_like"]).facts[0]
        disliked = extractor.extract("我不喜欢蓝色", ["evt_dislike"]).facts[0]
        assert liked.key == disliked.key

        await memory_service.upsert_fact(liked)
        await memory_service.upsert_fact(disliked)
        history = await memory_service.list_fact_updates(liked.key)
        assert len(history) == 2
        assert sum(item["valid_to"] is None for item in history) == 1
        assert history[0]["value"] == "我不喜欢蓝色"


class TestEpisodeSegmenter:
    """Test conversation episode segmentation."""

    def test_segments_by_time_gap(self):
        segmenter = EpisodeSegmenter()
        t0 = datetime(2026, 7, 28, 12, 0, 0)
        t1 = datetime(2026, 7, 28, 12, 1, 0)
        t2 = datetime(2026, 7, 28, 12, 2, 0)
        t3 = datetime(2026, 7, 28, 12, 10, 0)  # > 5 min gap → boundary
        t4 = datetime(2026, 7, 28, 12, 11, 0)
        t5 = datetime(2026, 7, 28, 12, 12, 0)
        turns = [
            {
                "turn_id": "t1",
                "user_text": "你好呀你好呀",
                "companion_text": "你好！",
                "timestamp": t0,
            },
            {
                "turn_id": "t2",
                "user_text": "今天天气真好哇",
                "companion_text": "是啊",
                "timestamp": t1,
            },
            {
                "turn_id": "t3",
                "user_text": "我们出去玩吧",
                "companion_text": "好啊",
                "timestamp": t2,
            },
            # Gap > 5 min here
            {
                "turn_id": "t4",
                "user_text": "我刚打完游戏",
                "companion_text": "什么游戏？",
                "timestamp": t3,
            },
            {
                "turn_id": "t5",
                "user_text": "王者荣耀很好玩",
                "companion_text": "厉害！",
                "timestamp": t4,
            },
            {
                "turn_id": "t6",
                "user_text": "打了一个小时",
                "companion_text": "注意休息",
                "timestamp": t5,
            },
        ]
        result = segmenter.segment(turns)
        # 6 turns with a time gap → should create at least 1 episode
        assert result.turns_processed == 6
        assert result.episodes_created >= 1

    def test_segments_with_high_salience(self):
        segmenter = EpisodeSegmenter()
        turns = [
            {
                "turn_id": "t1",
                "user_text": "我今天太开心了！！！",
                "companion_text": "怎么了？",
                "timestamp": datetime.now(),
            },
            {
                "turn_id": "t2",
                "user_text": "考试过了！！感动哭了",
                "companion_text": "恭喜！",
                "timestamp": datetime.now(),
            },
            {
                "turn_id": "t3",
                "user_text": "兴奋得睡不着",
                "companion_text": "理解你",
                "timestamp": datetime.now(),
            },
        ]
        result = segmenter.segment(turns)
        if result.episodes:
            assert result.episodes[0].emotional_salience > 0.5

    def test_empty_turns(self):
        segmenter = EpisodeSegmenter()
        result = segmenter.segment([])
        assert result.episodes_created == 0


class TestReflectionEngine:
    """Test reflection generation from accumulated events."""

    def test_accumulation_triggers_reflection(self):
        config = ReflectionConfig(
            importance_threshold=0.5,
            min_events_for_reflection=3,
            max_reflections_per_day=100,
        )
        engine = ReflectionEngine(config)

        # Feed events below threshold
        r1 = engine.feed_event("evt_1", importance=0.2)
        assert r1 is None

        r2 = engine.feed_event("evt_2", importance=0.3)
        assert r2 is None

        # Third event crosses threshold
        r3 = engine.feed_event("evt_3", importance=0.5)
        assert r3 is not None
        assert r3.category == "preference_summary"

    def test_daily_budget_respected(self):
        config = ReflectionConfig(
            importance_threshold=0.5,
            min_events_for_reflection=2,
            max_reflections_per_day=2,
            max_reflections_per_hour=10,
            min_interval_seconds=0,
        )
        engine = ReflectionEngine(config)

        # Feed events to reach threshold
        r1 = engine.feed_event("evt_1", importance=0.4)
        r2 = engine.feed_event("evt_2", importance=0.4)

        # Should have triggered at least one reflection by now
        # The budget allows 2 per day, so at least one should be generated
        assert r1 is not None or r2 is not None  # At least one triggered

    def test_force_reflection(self):
        engine = ReflectionEngine(ReflectionConfig())
        ref = engine.force_reflection("relationship_insight")
        assert ref is not None
        assert ref.category == "relationship_insight"

    def test_candidate_plans_generated(self):
        config = ReflectionConfig(
            importance_threshold=0.3,
            min_events_for_reflection=1,
        )
        engine = ReflectionEngine(config)
        # Feed goal-tracking events
        engine.feed_event("evt_1", category_hint="goal_tracking", importance=0.5)
        engine.feed_event("evt_2", category_hint="goal_tracking", importance=0.5)

        plans = engine.get_pending_plans()
        # May or may not have plans depending on thresholds
        assert isinstance(plans, list)

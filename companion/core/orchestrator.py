"""Companion Orchestrator — the central coordinator.

The orchestrator is the ONLY component that can:
1. Form replies (combining identity, memory, emotion, and LLM output)
2. Propose candidate actions (which the PolicyGate must approve)
3. Coordinate the fast-loop (real-time dialogue) and slow-loop (reflection)

It reads from all services but is the single writer for dialogue decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from companion.conversation import ConversationHistory, TurnEntry
from companion.core.event_bus import EventBus
from companion.core.expression_mapper import ExpressionMapper
from companion.core.policy_gate import PolicyGate
from companion.core.state_manager import StateManager
from companion.events.base import generate_ulid
from companion.events.conversation import (
    ConversationTurnCompletedEvent,
    ConversationTurnFailedEvent,
    ConversationTurnStartedEvent,
    LlmResponseGeneratedEvent,
)
from companion.memory.fact_extractor import FactExtractor
from companion.prompt_builder import PromptBuilder
from companion.providers.action import ActionProvider
from companion.providers.asr import ASRProvider
from companion.providers.avatar import AvatarProvider, AvatarState, BodyPose, FacialExpression
from companion.providers.memory import MemoryProvider
from companion.providers.model import LLMProvider, LLMRequest, LLMResponse
from companion.providers.perception import PerceptionProvider
from companion.providers.tts import TTSProvider

logger = logging.getLogger(__name__)
TurnFailureStage = Literal[
    "configuration",
    "asr",
    "generation",
    "tts",
    "playback",
    "persistence",
    "cancellation",
]


class CompanionOrchestrator:
    """Central coordinator for the virtual companion.

    This is the "brain" that:
    - Receives user input (voice/text)
    - Retrieves relevant memories and facts
    - Generates responses through the LLM
    - Synthesizes voice through TTS
    - Coordinates emotion, expression, and proactive behavior
    """

    def __init__(
        self,
        state_manager: StateManager,
        event_bus: EventBus,
        policy_gate: PolicyGate,
        llm_provider: LLMProvider | None = None,
        tts_provider: TTSProvider | None = None,
        asr_provider: ASRProvider | None = None,
        memory_provider: MemoryProvider | None = None,
        avatar_provider: AvatarProvider | None = None,
        action_provider: ActionProvider | None = None,
        perception_provider: PerceptionProvider | None = None,
    ) -> None:
        self.state = state_manager
        self.bus = event_bus
        self.policy = policy_gate

        self._llm = llm_provider
        self._tts = tts_provider
        self._asr = asr_provider
        self._memory = memory_provider
        self._avatar = avatar_provider
        self._action = action_provider
        self._perception = perception_provider

        self._running = False
        self._shutdown_clean = False
        self._session_id: str = ""
        self._history: ConversationHistory = ConversationHistory()
        self._turn_sequence: int = 0
        self._fact_extractor: FactExtractor = FactExtractor()
        self._prompt_builder: PromptBuilder = PromptBuilder()
        self._expression_mapper = ExpressionMapper()
        self._text_turn_lock = asyncio.Lock()

    # ── Provider setters (for late binding / testing) ─────────────────

    def set_llm(self, provider: LLMProvider) -> None:
        self._llm = provider

    def set_tts(self, provider: TTSProvider) -> None:
        self._tts = provider

    def set_asr(self, provider: ASRProvider) -> None:
        self._asr = provider

    def set_memory(self, provider: MemoryProvider) -> None:
        self._memory = provider

    def set_avatar(self, provider: AvatarProvider) -> None:
        self._avatar = provider

    def set_action(self, provider: ActionProvider) -> None:
        self._action = provider

    def set_perception(self, provider: PerceptionProvider) -> None:
        self._perception = provider

    # ── Lifecycle ─────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def shutdown_clean(self) -> bool:
        return self._shutdown_clean

    async def startup(self) -> bool:
        """Initialize providers and return whether required services are ready."""
        logger.info("Companion orchestrator starting up...")
        self._running = False
        self._session_id = f"sess_{generate_ulid()}"

        # Health-check all providers and aggregate results
        health_results: dict[str, str] = {}
        for name, provider in [
            ("LLM", self._llm),
            ("TTS", self._tts),
            ("ASR", self._asr),
            ("Memory", self._memory),
            ("Avatar", self._avatar),
            ("Action", self._action),
            ("Perception", self._perception),
        ]:
            if provider:
                try:
                    health = await provider.health_check()
                    health_results[name] = str(health)
                    logger.info("%s provider health: %s", name, health)
                except Exception:
                    health_results[name] = "error"
                    logger.exception("%s provider health check failed", name)

        healthy_count = sum(1 for v in health_results.values() if v == "healthy")
        logger.info(
            "Companion orchestrator started. Provider health: %d/%d healthy (%s)",
            healthy_count,
            len(health_results),
            health_results,
        )
        if not self._llm or health_results.get("LLM") != "healthy":
            logger.error("Companion startup blocked: required LLM provider is not healthy")
            return False
        if self._memory and health_results.get("Memory") != "healthy":
            logger.error("Companion startup blocked: configured memory provider is not healthy")
            return False
        if self._avatar and health_results.get("Avatar") != "healthy":
            logger.error("Companion startup blocked: configured avatar provider is not healthy")
            return False
        if self._action and health_results.get("Action") != "healthy":
            logger.error("Companion startup blocked: configured action provider is not healthy")
            return False
        if self._avatar:
            model_id = self.state.identity.avatar_model_id
            if model_id:
                try:
                    validation_errors = await self._avatar.validate_model(model_id)
                    if validation_errors:
                        logger.error(
                            "Companion startup blocked: avatar model validation failed: %s",
                            validation_errors,
                        )
                        return False
                    if not await self._avatar.load_model(model_id):
                        logger.error("Companion startup blocked: avatar model could not be loaded")
                        return False
                except Exception:
                    logger.exception("Companion startup blocked: avatar model setup failed")
                    return False
            if not await self._sync_avatar_state(strict=True):
                return False

        self._running = True
        return True

    async def shutdown(self) -> None:
        """Gracefully shutdown all providers."""
        logger.info("Companion orchestrator shutting down...")
        self._running = False
        self._shutdown_clean = False
        providers_clean = True

        for name, provider in [
            ("LLM", self._llm),
            ("TTS", self._tts),
            ("ASR", self._asr),
            ("Memory", self._memory),
            ("Avatar", self._avatar),
            ("Action", self._action),
            ("Perception", self._perception),
        ]:
            if provider:
                try:
                    await provider.shutdown()
                    logger.debug("%s provider shut down.", name)
                except Exception:
                    providers_clean = False
                    logger.exception("Error shutting down %s provider", name)

        self._shutdown_clean = providers_clean
        logger.info("Companion orchestrator shut down.")

    # ── Fast Loop: Real-time dialogue ─────────────────────────────────

    async def process_user_input(
        self, text: str, turn_id: str = "", session_id: str = ""
    ) -> dict[str, Any]:
        """Process a user text input through the full fast-loop pipeline.

        This is the primary entry point for dialogue. It:
        1. Updates emotional state based on input sentiment
        2. Retrieves relevant memory facts and episodes
        3. Builds the full system prompt (identity + memory + emotion + history)
        4. Generates a response through the LLM
        5. Stores the turn in conversation history
        6. Extracts facts from user input to memory
        7. Returns the response text and metadata

        Returns:
            Dict with keys: response_text, emotion, latency_ms, model_id, turn_id
        """
        async with self._text_turn_lock:
            return await self._process_user_input_serialized(text, turn_id, session_id)

    async def _process_user_input_serialized(
        self, text: str, turn_id: str, session_id: str
    ) -> dict[str, Any]:
        """Run one text turn against a stable history and sequence number."""
        if not turn_id:
            turn_id = f"turn_{generate_ulid()}"
        if not self._session_id:
            self._session_id = session_id or f"sess_{generate_ulid()}"
        if session_id:
            self._session_id = session_id

        self._turn_sequence += 1
        t_start = time.time()

        try:
            await self.bus.publish(
                ConversationTurnStartedEvent(
                    session_id=self._session_id,
                    turn_id=turn_id,
                    turn_sequence=self._turn_sequence,
                    input_modality="text",
                )
            )
        except asyncio.CancelledError:
            # EventBus completes an accepted start commit before cancellation propagates.
            await self._publish_turn_failed(
                turn_id,
                stage="cancellation",
                error_type="cancelled",
                retryable=True,
                started_at=t_start,
            )
            raise

        if not self._llm:
            await self._publish_turn_failed(
                turn_id,
                stage="configuration",
                error_type="llm_not_configured",
                retryable=False,
                started_at=t_start,
            )
            return {
                "response_text": "[LLM provider not configured]",
                "emotion": "neutral",
                "turn_id": turn_id,
            }

        try:
            response = await self.prepare_response(text, turn_id)
        except asyncio.CancelledError:
            await self._publish_turn_failed(
                turn_id,
                stage="cancellation",
                error_type="cancelled",
                retryable=True,
                started_at=t_start,
            )
            raise
        except Exception as exc:
            logger.exception("LLM generation failed")
            await self._publish_turn_failed(
                turn_id,
                stage="generation",
                error_type=type(exc).__name__,
                retryable=True,
                started_at=t_start,
            )
            return {
                "response_text": "抱歉，我暂时无法回应…",
                "emotion": "concerned",
                "latency_ms": int((time.time() - t_start) * 1000),
                "model_id": "error",
                "turn_id": turn_id,
            }

        try:
            await self.bus.publish(
                LlmResponseGeneratedEvent(
                    turn_id=turn_id,
                    response_text=response.text,
                    model_id=response.model_id,
                    model_provider=response.model_provider,
                    time_to_first_token_ms=response.time_to_first_token_ms,
                    total_latency_ms=response.total_latency_ms,
                    token_count=response.token_count,
                )
            )

        except asyncio.CancelledError:
            await self._publish_turn_failed(
                turn_id,
                stage="cancellation",
                error_type="cancelled",
                retryable=True,
                started_at=t_start,
            )
            raise
        except Exception as exc:
            logger.exception("Conversation event persistence failed")
            await self._publish_turn_failed(
                turn_id,
                stage="persistence",
                error_type=type(exc).__name__,
                retryable=True,
                started_at=t_start,
            )
            return {
                "response_text": "抱歉，我暂时无法回应…",
                "emotion": "concerned",
                "latency_ms": int((time.time() - t_start) * 1000),
                "model_id": "error",
                "turn_id": turn_id,
            }

        completed_event = ConversationTurnCompletedEvent(
            turn_id=turn_id,
            session_id=self._session_id,
            turn_sequence=self._turn_sequence,
            user_text=text,
            companion_text=response.text,
            companion_full_text=response.text,
            total_latency_ms=response.total_latency_ms,
            model_id=response.model_id,
        )
        try:
            await self.bus.publish(completed_event)
        except asyncio.CancelledError:
            # EventBus finishes an accepted publish before cancellation propagates.
            await self._commit_response_uncancellable(text, response, completed_event.event_id)
            raise
        except Exception as exc:
            logger.exception("Conversation completion persistence failed")
            await self._publish_turn_failed(
                turn_id,
                stage="persistence",
                error_type=type(exc).__name__,
                retryable=True,
                started_at=t_start,
            )
            return {
                "response_text": "抱歉，我暂时无法回应…",
                "emotion": "concerned",
                "latency_ms": int((time.time() - t_start) * 1000),
                "model_id": "error",
                "turn_id": turn_id,
            }

        await self._commit_response_uncancellable(text, response, completed_event.event_id)

        return {
            "response_text": response.text,
            "emotion": self.state.dominant_emotion(),
            "latency_ms": response.total_latency_ms,
            "model_id": response.model_id,
            "turn_id": turn_id,
        }

    async def _commit_response_uncancellable(
        self, user_text: str, response: LLMResponse, source_event_id: str
    ) -> None:
        commit_task = asyncio.create_task(
            self.commit_response(
                user_text,
                response,
                source_event_id,
                communicated_text=response.text,
            )
        )
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            await commit_task
            raise

    async def _publish_turn_failed(
        self,
        turn_id: str,
        *,
        stage: TurnFailureStage,
        error_type: str,
        retryable: bool,
        started_at: float,
    ) -> None:
        await self.bus.publish(
            ConversationTurnFailedEvent(
                turn_id=turn_id,
                session_id=self._session_id,
                turn_sequence=self._turn_sequence,
                stage=stage,
                error_type=error_type,
                retryable=retryable,
                elapsed_ms=max(0, int((time.time() - started_at) * 1000)),
            )
        )

    async def prepare_response(self, text: str, turn_id: str) -> LLMResponse:
        """Generate a contextual response without committing conversation history."""
        if not self._llm:
            raise RuntimeError("LLM provider not configured")

        sentiment = self._detect_sentiment(text)
        self._apply_sentiment_delta(sentiment)
        await self._sync_avatar_state(strict=False)
        facts = []
        episodes = []
        if self._memory:
            try:
                facts = await self._memory.search_facts(text, limit=5)
                episodes = await self._memory.search_episodes(text, limit=3)
            except Exception:
                logger.exception("Memory retrieval failed, continuing without")

        system_prompt = self._prompt_builder.build(
            identity=self.state.identity,
            affect=self.state.affect,
            relationship=self.state.relationship,
            history=self._history,
            facts=facts,
            episodes=episodes,
        )
        request = LLMRequest(
            messages=self._history.build_messages(system_prompt, text),
            system_prompt=system_prompt,
            turn_id=turn_id,
            max_tokens=512,
            temperature=0.7,
        )
        return await self._llm.generate(request)

    async def commit_response(
        self,
        user_text: str,
        response: LLMResponse,
        source_event_id: str,
        *,
        communicated_text: str,
    ) -> None:
        """Commit only the response text that was actually communicated."""
        self._history.add_turn(
            TurnEntry(
                turn_id=response.turn_id,
                user_text=user_text,
                companion_text=communicated_text,
                model_id=response.model_id,
                latency_ms=response.total_latency_ms,
            )
        )
        await self._extract_and_store_facts(user_text, source_event_id)
        self.state.apply_time_decay(3.0)
        await self._sync_avatar_state(strict=False)

    async def cancel_turn(self, turn_id: str) -> bool:
        """Cancel generation delegated through this orchestrator."""
        return await self._llm.cancel(turn_id) if self._llm else False

    # ── Slow Loop: Reflection and planning ────────────────────────────

    async def run_reflection_cycle(self) -> list[dict[str, Any]]:
        """Run one cycle of the slow-loop: reflection and candidate plan generation.

        This is an async background task, NOT part of real-time dialogue.
        It should be triggered periodically or when importance thresholds are met.
        """
        return []

    # ── State helpers ─────────────────────────────────────────────────

    def get_conversation_history(self) -> ConversationHistory:
        return self._history

    def get_conversation_summary(self) -> str:
        return self._history.summary()

    @property
    def turn_count(self) -> int:
        return len(self._history)

    # ── Internal ──────────────────────────────────────────────────────

    def _detect_sentiment(self, text: str) -> float:
        """Quick sentiment heuristic: positive words → +delta, negative → -delta.

        Returns a value in [-0.3, 0.3] representing the emotional delta.
        """
        positive_words = {
            "开心",
            "高兴",
            "好",
            "太棒",
            "哈哈",
            "喜欢",
            "爱",
            "😊",
            "棒",
            "赞",
            "不错",
            "厉害",
            "兴奋",
            "激动",
            "yes",
            "great",
            "nice",
            "good",
            "love",
            "happy",
            "awesome",
            "wow",
        }
        negative_words = {
            "难过",
            "伤心",
            "生气",
            "烦",
            "累",
            "讨厌",
            "恨",
            "😢",
            "哭",
            "崩溃",
            "焦虑",
            "怕",
            "糟糕",
            "不行",
            "no",
            "bad",
            "sad",
            "hate",
            "terrible",
            "awful",
            "tired",
            "upset",
        }

        text_lower = text.lower()
        pos_count = sum(1 for w in positive_words if w in text_lower or w in text)
        neg_count = sum(1 for w in negative_words if w in text_lower or w in text)

        if pos_count > neg_count:
            return min(0.15, pos_count * 0.05)
        elif neg_count > pos_count:
            return max(-0.15, -neg_count * 0.05)
        return 0.0

    def _apply_sentiment_delta(self, sentiment: float) -> None:
        """Apply a small emotional delta based on user sentiment."""
        if abs(sentiment) > 0.01:
            self.state.apply_affect_event(
                delta_valence=sentiment,
                delta_arousal=abs(sentiment) * 0.3,
                delta_energy=-0.02,  # Small energy drain per turn
            )

    async def _extract_and_store_facts(self, text: str, source_event_id: str) -> None:
        """Extract facts from user input and store in memory."""
        if not self._memory:
            return
        try:
            result = self._fact_extractor.extract(text, [source_event_id])
            for fact in result.facts:
                await self._memory.upsert_fact(fact)
            if result.facts:
                logger.debug("Extracted %d facts from event %s", len(result.facts), source_event_id)
        except Exception:
            logger.exception("Fact extraction failed for event %s", source_event_id)

    async def _sync_avatar_state(self, *, strict: bool) -> bool:
        """Push one affect-derived snapshot while keeping Python authoritative."""
        if not self._avatar:
            return True
        affect = self.state.affect
        snapshot = self._expression_mapper.map(affect)
        gesture = max(snapshot.gestures, key=lambda item: item.priority, default=None)
        avatar_state = AvatarState(
            expression=FacialExpression(
                expression_id=snapshot.facial.expression_id,
                intensity=snapshot.facial.expression_intensity,
                mouth_open=snapshot.facial.mouth_open,
                eye_open=snapshot.facial.eye_open,
                brow_raise=snapshot.facial.brow_raise,
                cheek_raise=snapshot.facial.cheek_raise,
            ),
            pose=BodyPose(
                gesture_id=gesture.gesture_id if gesture else None,
                gesture_intensity=gesture.intensity if gesture else 0.5,
            ),
            valence=affect.valence,
            arousal=affect.arousal,
            energy=affect.energy,
        )
        try:
            await self._avatar.update_state(avatar_state)
            await self._avatar.set_proactive_level(snapshot.proactive_level_hint)
            return True
        except Exception:
            if strict:
                logger.exception("Companion startup blocked: initial avatar state sync failed")
            else:
                logger.exception("Avatar state sync failed; dialogue will continue")
            return False

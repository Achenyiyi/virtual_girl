"""Fact Extractor — extracts structured facts from conversation turns.

Implements Layer 3 of the five-layer memory system:
- Extracts key-value preference/identity facts from dialogue
- Assigns confidence scores based on extraction certainty
- Manages fact validity ranges (updates close old facts)
- Tags facts with categories: preference, identity, relationship, schedule

Key design: facts are always linked to source events for audit/correction.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from companion.events.base import generate_ulid
from companion.providers.memory import SemanticFact

logger = logging.getLogger(__name__)


@dataclass
class FactExtractionResult:
    """Result of extracting facts from conversation text."""

    facts: list[SemanticFact]
    extraction_method: str = "structured"


class FactExtractor:
    """Extracts structured facts from conversation text using pattern matching.

    For Phase 2, this uses rule-based extraction patterns. In production,
    this would be backed by an LLM call for nuanced extraction, with the
    rule-based system as a fast pre-filter.
    """

    # Pattern-based extraction keywords and their categories
    PREFERENCE_PATTERNS: list[tuple[str, str]] = [
        ("喜欢", "preference"),
        ("讨厌", "preference"),
        ("不喜欢", "preference"),
        ("最爱", "preference"),
        ("偏好", "preference"),
        ("更喜欢", "preference"),
        ("喜欢听", "preference_music"),
        ("喜欢看", "preference_media"),
        ("喜欢玩", "preference_gaming"),
        ("喜欢吃", "preference_food"),
        ("喜欢喝", "preference_food"),
    ]

    IDENTITY_PATTERNS: list[tuple[str, str]] = [
        ("我是", "identity"),
        ("我叫", "identity_name"),
        ("我是做", "identity_job"),
        ("我今年", "identity_age"),
        ("我住在", "identity_location"),
        ("我在", "identity_location"),
        ("我的名字是", "identity_name"),
    ]

    RELATIONSHIP_PATTERNS: list[tuple[str, str]] = [
        ("我女", "relationship"),
        ("我男", "relationship"),
        ("我老公", "relationship"),
        ("我老婆", "relationship"),
        ("我孩子", "relationship"),
        ("我家人", "relationship"),
        ("我朋友", "relationship"),
        ("我同事", "relationship"),
    ]

    SCHEDULE_PATTERNS: list[tuple[str, str]] = [
        ("明天要", "schedule"),
        ("下周", "schedule"),
        ("今天要", "schedule"),
        ("周末", "schedule"),
        ("计划", "schedule"),
        ("打算", "schedule"),
        ("要去", "schedule"),
        ("要考试", "schedule"),
        ("面试", "schedule"),
    ]

    # Longest patterns win so "不喜欢" is not also recorded as "喜欢".
    ALL_PATTERNS: tuple[tuple[str, str], ...] = tuple(
        sorted(
            PREFERENCE_PATTERNS
            + IDENTITY_PATTERNS
            + RELATIONSHIP_PATTERNS
            + SCHEDULE_PATTERNS,
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )

    def extract(self, text: str, source_event_ids: list[str]) -> FactExtractionResult:
        """Extract facts from a user's conversation text.

        Args:
            text: The user's message text
            source_event_ids: Source events this text came from

        Returns:
            FactExtractionResult with extracted facts
        """
        facts: list[SemanticFact] = []

        matched_spans: list[tuple[int, int]] = []
        for pattern, category in self.ALL_PATTERNS:
            pattern_start = text.find(pattern)
            if pattern_start >= 0:
                pattern_span = (pattern_start, pattern_start + len(pattern))
                if any(
                    pattern_span[0] < end and pattern_span[1] > start
                    for start, end in matched_spans
                ):
                    continue
                context = self._extract_context(text, pattern, pattern_start)
                if context:
                    matched_spans.append(pattern_span)
                    # Map to standardized category
                    std_category = self._standardize_category(category)

                    fact = SemanticFact(
                        fact_id=f"fact_{generate_ulid()}",
                        key=self._make_key(pattern, context),
                        value=context,
                        category=std_category,
                        confidence=self._compute_confidence(pattern),
                        source_event_ids=list(source_event_ids),
                        extraction_method="structured",
                    )
                    facts.append(fact)

        return FactExtractionResult(facts=facts)

    def _extract_context(self, text: str, pattern: str, pattern_start: int) -> str:
        """Extract the relevant phrase around a pattern match."""
        # Take from pattern start to end of sentence or 50 chars
        start = max(0, pattern_start - 5)
        end = min(len(text), pattern_start + len(pattern) + 30)

        # Try to end at sentence boundary
        context = text[start:end]
        for sep in "。！？，,!?…\n":
            sep_idx = context.find(sep, len(pattern) + 5)
            if sep_idx > 0:
                context = context[: sep_idx + 1]
                break

        return context.strip()

    _KEY_MAP = {
        "喜欢": "preference_like",
        "讨厌": "preference_dislike",
        "不喜欢": "preference_dislike",
        "最爱": "preference_favorite",
        "更喜欢": "preference_prefer",
        "我是": "identity",
        "我叫": "identity_name",
        "我是做": "identity_job",
        "我今年": "identity_age",
        "我住在": "identity_location",
        "明天要": "schedule_tomorrow",
        "今天要": "schedule_today",
        "下周": "schedule_next_week",
        "要去": "plan_go",
        "打算": "plan",
        "计划": "plan",
    }
    _PREFERENCE_PATTERN_SET = frozenset(item[0] for item in PREFERENCE_PATTERNS)

    def _make_key(self, pattern: str, value: str) -> str:
        """Generate a normalized fact key."""
        # Keys represent semantic slots, not values. A changed value therefore
        # closes the previous version instead of creating a second active fact.
        prefix = self._KEY_MAP.get(pattern, f"fact_{pattern}")
        if pattern in self._PREFERENCE_PATTERN_SET:
            subject = value.split(pattern, 1)[-1]
            subject = re.sub(r"[。！？，,!?…\s]+$", "", subject).strip()
            subject_hash = hashlib.sha256(subject.encode()).hexdigest()[:12]
            return f"preference_{subject_hash}"
        if prefix.startswith(("identity", "schedule", "plan")):
            return prefix
        content_hash = hashlib.sha256(value.encode()).hexdigest()[:12]
        return f"{prefix}_{content_hash}"

    def _standardize_category(self, raw: str) -> str:
        """Map raw pattern category to standard memory category."""
        if raw.startswith("preference"):
            return "preference"
        if raw.startswith("identity"):
            return "identity"
        if raw.startswith("relationship"):
            return "relationship"
        if raw.startswith("schedule") or raw.startswith("plan"):
            return "schedule"
        return "general"

    def _compute_confidence(self, pattern: str) -> float:
        """Compute confidence based on pattern explicitness."""
        # Explicit statements ("我是", "我叫") have higher confidence
        explicit_patterns = {"我是", "我叫", "我的名字是", "我今年", "我住在"}
        if pattern in explicit_patterns:
            return 0.9

        # Preference statements
        if pattern in {"喜欢", "讨厌", "不喜欢", "最爱", "更喜欢"}:
            return 0.75

        # Schedule/plan mentions
        if pattern in {"明天要", "今天要", "下周", "要去", "打算", "计划"}:
            return 0.6

        # Default
        return 0.5

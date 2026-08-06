"""Episode Segmenter — groups conversation turns into episodic memories.

Implements Layer 4 of the five-layer memory system:
- Segments conversation streams into meaningful episodes
- Assigns emotional salience scores
- Tags episodes for retrieval
- Links episodes to their source turns

Segmentation strategy:
- Topic boundaries (when conversation topic shifts significantly)
- Time gaps (> 5 minutes between turns = new episode boundary)
- Session boundaries (new session = new episode)
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from companion.events.base import generate_ulid
from companion.providers.memory import Episode

logger = logging.getLogger(__name__)

# Try to import jieba for CJK tokenization; fall back to character n-grams
_HAS_JIEBA = importlib.util.find_spec("jieba") is not None
jieba: Any = importlib.import_module("jieba") if _HAS_JIEBA else None
if not _HAS_JIEBA:
    logger.info("jieba not installed; falling back to character n-gram tokenization for Chinese")


@dataclass
class EpisodeCandidate:
    """A candidate episode to be created."""

    turn_ids: list[str] = field(default_factory=list)
    user_texts: list[str] = field(default_factory=list)
    companion_texts: list[str] = field(default_factory=list)
    first_turn_time: datetime | None = None
    last_turn_time: datetime | None = None
    emotional_salience: float = 0.5
    topic_keywords: set[str] = field(default_factory=set)


@dataclass
class SegmentationResult:
    """Result of segmenting a conversation into episodes."""

    episodes: list[Episode]
    turns_processed: int
    episodes_created: int


class EpisodeSegmenter:
    """Segments conversation turns into episodic memories.

    Boundary detection uses:
    1. Time gaps between turns
    2. Topic shift detection (simple keyword overlap)
    3. Emotional salience from user sentiment words
    """

    # Time gap to create a new episode boundary (seconds)
    TIME_GAP_THRESHOLD_SECONDS: int = 300  # 5 minutes

    # Minimum turns for a meaningful episode
    MIN_TURNS_PER_EPISODE: int = 3

    # Emotional keywords for salience scoring
    HIGH_SALIENCE_WORDS: set[str] = {
        "太棒了",
        "好开心",
        "兴奋",
        "激动",
        "感动",
        "哭了",
        "笑死",
        "难过",
        "伤心",
        "生气",
        "气死",
        "崩溃",
        "震撼",
        "amazing",
    }
    MEDIUM_SALIENCE_WORDS: set[str] = {
        "不错",
        "还行",
        "可以",
        "挺好",
        "有趣",
        "有意思",
        "无聊",
        "一般",
        "还行吧",
    }

    def segment(
        self,
        turns: list[dict[str, Any]],
        topic_shift_threshold: float = 0.3,
    ) -> SegmentationResult:
        """Segment a list of turns into episodes.

        Args:
            turns: List of dicts with keys: turn_id, user_text, companion_text, timestamp
            topic_shift_threshold: Jaccard similarity below which = new episode

        Returns:
            SegmentationResult with created episodes
        """
        if not turns:
            return SegmentationResult(episodes=[], turns_processed=0, episodes_created=0)

        candidates: list[EpisodeCandidate] = []
        current = EpisodeCandidate()

        for i, turn in enumerate(turns):
            turn_time = turn.get("timestamp")
            turn_id = turn.get("turn_id", f"turn_{i}")
            user_text = turn.get("user_text", "")
            companion_text = turn.get("companion_text", "")

            # Check for time gap boundary
            if current.first_turn_time and turn_time:
                gap = (
                    (turn_time - current.last_turn_time).total_seconds()
                    if current.last_turn_time
                    else 0
                )
                if (
                    gap > self.TIME_GAP_THRESHOLD_SECONDS
                    and len(current.turn_ids) >= self.MIN_TURNS_PER_EPISODE
                ):
                    candidates.append(current)
                    current = EpisodeCandidate()
                elif gap > self.TIME_GAP_THRESHOLD_SECONDS:
                    # Short episode, merge with next
                    current = EpisodeCandidate()

            # Check for topic shift boundary
            if current.topic_keywords and user_text:
                current_keywords = self._extract_keywords(user_text)
                overlap = self._keyword_overlap(current.topic_keywords, current_keywords)
                if (
                    overlap < topic_shift_threshold
                    and len(current.turn_ids) >= self.MIN_TURNS_PER_EPISODE
                ):
                    candidates.append(current)
                    current = EpisodeCandidate()

            # Add turn to current candidate
            current.turn_ids.append(turn_id)
            current.user_texts.append(user_text)
            current.companion_texts.append(companion_text)
            if turn_time:
                if not current.first_turn_time:
                    current.first_turn_time = turn_time
                current.last_turn_time = turn_time

            # Update keywords
            current.topic_keywords |= self._extract_keywords(user_text)

            # Update salience
            current.emotional_salience = max(
                current.emotional_salience,
                self._compute_salience(user_text, companion_text),
            )

        # Don't forget the last candidate
        if current.turn_ids:
            candidates.append(current)

        # Convert candidates to episodes (only those with enough turns)
        episodes = []
        for c in candidates:
            if len(c.turn_ids) >= self.MIN_TURNS_PER_EPISODE:
                # Generate title from first few user messages
                title = self._generate_title(c)
                summary = self._generate_summary(c)

                episodes.append(
                    Episode(
                        episode_id=f"ep_{generate_ulid()}",
                        title=title,
                        summary=summary,
                        turn_ids=list(c.turn_ids),
                        tags=list(c.topic_keywords)[:10],
                        emotional_salience=c.emotional_salience,
                        occurred_at=c.first_turn_time or datetime.now(UTC),
                    )
                )

        return SegmentationResult(
            episodes=episodes,
            turns_processed=len(turns),
            episodes_created=len(episodes),
        )

    # ── Internal helpers ──────────────────────────────────────────────

    _STOP_WORDS = frozenset(
        {
            "的",
            "了",
            "是",
            "我",
            "你",
            "他",
            "她",
            "它",
            "们",
            "这",
            "那",
            "在",
            "有",
            "不",
            "和",
            "就",
            "都",
            "也",
            "还",
            "要",
            "会",
            "吧",
            "吗",
            "呢",
            "啊",
            "哦",
            "嗯",
            "说",
            "想",
            "看",
            "去",
            "来",
            "做",
            "好",
            "很",
            "对",
            "没",
            "一",
            "个",
        }
    )

    def _extract_keywords(self, text: str) -> set[str]:
        """Extract keywords from text for topic tracking.

        Uses jieba for Chinese tokenization if available;
        falls back to character bigram extraction otherwise.
        """
        if not text:
            return set()

        stop_words = self._STOP_WORDS
        words = set()

        if _HAS_JIEBA:
            # Use jieba for proper Chinese word segmentation
            tokens = jieba.cut(text)
            for w in tokens:
                w = w.strip("，。！？、…“”'《》（） \t\n\r")
                if w and w not in stop_words and len(w) > 1:
                    words.add(w)
        else:
            # Fallback: character bigrams (works for CJK) + whitespace split
            # Extract bigrams from Chinese characters
            cleaned = "".join(c for c in text if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
            for i in range(len(cleaned) - 1):
                bigram = cleaned[i : i + 2]
                if bigram not in stop_words:
                    words.add(bigram)
            # Also extract whitespace-delimited words (for Latin text)
            for w in text.split():
                w = w.strip("，。！？、…“”'《》（） \t\n\r")
                if w and w not in stop_words and len(w) > 1:
                    words.add(w)

        return words

    def _keyword_overlap(self, set1: set[str], set2: set[str]) -> float:
        """Jaccard similarity between two keyword sets."""
        if not set1 or not set2:
            return 0.0
        intersection = set1 & set2
        union = set1 | set2
        return len(intersection) / len(union) if union else 0.0

    def _compute_salience(self, user_text: str, companion_text: str) -> float:
        """Compute emotional salience from keyword matching."""
        combined = user_text + companion_text
        high_count = sum(1 for w in self.HIGH_SALIENCE_WORDS if w in combined)
        medium_count = sum(1 for w in self.MEDIUM_SALIENCE_WORDS if w in combined)

        if high_count > 0:
            return min(1.0, 0.5 + high_count * 0.25)
        if medium_count > 0:
            return min(0.7, 0.3 + medium_count * 0.15)
        return 0.3  # Default baseline

    def _generate_title(self, candidate: EpisodeCandidate) -> str:
        """Generate a short title from user texts."""
        # Take first meaningful user utterance
        for text in candidate.user_texts:
            text = text.strip()
            if len(text) > 3:
                return text[:40] + ("..." if len(text) > 40 else "")
        return "对话片段"

    def _generate_summary(self, candidate: EpisodeCandidate) -> str:
        """Generate a brief summary of the episode."""
        turns = len(candidate.turn_ids)
        topics = (
            ", ".join(sorted(candidate.topic_keywords)[:5])
            if candidate.topic_keywords
            else "日常聊天"
        )
        salience = (
            "重要"
            if candidate.emotional_salience > 0.7
            else "普通"
            if candidate.emotional_salience > 0.3
            else "轻松"
        )
        return f"{salience}对话（{turns}轮）：涉及 {topics}"

"""Conversation History — multi-turn context window management.

Handles:
- Storing conversation turns in order
- Truncating older turns when the context window fills
- Building message lists for LLM API calls
- Estimating token counts for budget management
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnEntry:
    """A single conversation turn (user + companion)."""

    turn_id: str
    user_text: str
    companion_text: str
    model_id: str = ""
    latency_ms: int = 0


@dataclass
class ConversationHistory:
    """Ordered conversation history with context window management.

    Truncates oldest turns first to stay within the token budget.
    """

    max_turns: int = 50
    max_history_tokens: int = 4000  # Budget for recent conversation
    _turns: list[TurnEntry] = field(default_factory=list)

    def add_turn(self, entry: TurnEntry) -> None:
        """Append a turn and trim if needed."""
        self._turns.append(entry)
        self._trim()

    def get_recent_turns(self, n: int | None = None) -> list[TurnEntry]:
        """Return the most recent n turns (default: all within budget)."""
        if n is not None:
            return self._turns[-n:]
        return list(self._turns)

    def build_messages(self, system_prompt: str, current_user_text: str) -> list[dict[str, str]]:
        """Build a message list for the LLM API call.

        Returns a list of {"role": ..., "content": ...} dicts.
        The system prompt is placed as the first message.
        Recent turns are included as user/assistant pairs.
        """
        messages: list[dict[str, str]] = []

        # System prompt
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Recent conversation history
        for turn in self._turns[-20:]:  # Last 20 turns max
            if turn.user_text:
                messages.append({"role": "user", "content": turn.user_text})
            if turn.companion_text:
                messages.append({"role": "assistant", "content": turn.companion_text})

        # Current user input
        messages.append({"role": "user", "content": current_user_text})

        return messages

    def _trim(self) -> None:
        """Remove oldest turns until within token budget."""
        while self._estimated_tokens() > self.max_history_tokens and len(self._turns) > 1:
            self._turns.pop(0)
        # Also enforce max turn count
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns :]

    def _estimated_tokens(self) -> int:
        """Rough token estimation (4 chars ≈ 1 token for Chinese/English text)."""
        total = 0
        for t in self._turns:
            total += len(t.user_text) // 3 + len(t.companion_text) // 3
        return total

    def summary(self) -> str:
        """Return a one-line summary of the conversation so far."""
        if not self._turns:
            return "（尚无对话历史）"
        last = self._turns[-3:]
        return "；".join(
            f"用户: {t.user_text[:30]}{'…' if len(t.user_text) > 30 else ''}" for t in last
        )

    def __len__(self) -> int:
        return len(self._turns)

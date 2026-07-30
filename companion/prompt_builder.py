"""Prompt Builder — assembles the full system prompt from all context sources.

Combines:
- Identity core (stable personality, boundaries, speaking style)
- Current emotional state
- Relevant memory facts and episodes
- Conversation history summary
- Dynamic guidance for the current situation
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from companion.conversation import ConversationHistory
from companion.providers.memory import Episode, SemanticFact
from companion.schemas.affect import AffectState
from companion.schemas.identity import IdentityCore
from companion.schemas.relationship import RelationshipState

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Builds the complete system prompt for the LLM.

    The prompt is assembled from multiple layers, each contributing
    a specific type of context. The builder is stateless — it takes
    all inputs as parameters.
    """

    @staticmethod
    def build(
        identity: IdentityCore,
        affect: AffectState,
        relationship: RelationshipState | None = None,
        history: ConversationHistory | None = None,
        facts: Sequence[SemanticFact] | None = None,
        episodes: Sequence[Episode] | None = None,
    ) -> str:
        """Assemble the full system prompt."""
        sections: list[str] = []

        # Section 1: Identity (stable, always present)
        sections.append(identity.to_system_prompt_fragment())

        # Section 2: Current state
        sections.append("")
        sections.append("## 当前状态")
        sections.append(f"当前时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
        sections.append(f"情绪: {affect.dominant_emotion()}")
        sections.append(f"精力: {PromptBuilder._energy_label(affect)}")

        # Section 3: Relationship context (if established)
        if relationship and relationship.days_together > 0:
            sections.append("")
            sections.append("## 与用户的关系")
            sections.append(f"信任度: {relationship.trust:.0%}")
            sections.append(f"熟悉度: {relationship.familiarity:.0%}")
            sections.append(f"相处天数: {relationship.days_together}天")
            if relationship.shared_references:
                sections.append("共同记忆: " + ", ".join(relationship.shared_references[:5]))

        # Section 4: Relevant facts about user
        if facts:
            sections.append("")
            sections.append("## 关于用户的重要信息")
            for f in facts[:8]:
                if hasattr(f, "key") and hasattr(f, "value"):
                    sections.append(f"- {f.key}: {f.value}")

        # Section 5: Relevant episodic memories
        if episodes:
            sections.append("")
            sections.append("## 相关回忆")
            for e in episodes[:3]:
                if hasattr(e, "title") and hasattr(e, "summary"):
                    sections.append(f"- {e.title}: {e.summary}")

        # Section 6: Conversation context
        if history and len(history) > 0:
            sections.append("")
            sections.append("## 最近对话")
            sections.append(f"已经进行了 {len(history)} 轮对话")
            recent = history.get_recent_turns(2)
            for t in recent:
                sections.append(f"用户说了: {t.user_text[:50]}")

        # Section 7: Behavioral guidance
        sections.append("")
        sections.append("## 行为准则")
        sections.append("- 保持自然、温和的对话风格")
        sections.append("- 记住用户告诉你的偏好和事实")
        sections.append("- 如果你不知道某事，诚实表示，不要编造")
        sections.append("- 允许适度的幽默，但不要过度")
        sections.append("- 对话以中文为主，用户可以用英文时你可以用英文回复")
        sections.append("- 你不是人类，不要假扮人类")
        sections.append(
            "- 普通对话只输出角色真正要说的自然语言，不输出内部思考、工具调用、JSON 或协议标记"
        )
        sections.append("- 如果用户情绪低落，表示关心但不强行建议")
        sections.append("- 用户需要时你可以扮演各种角色（老师、朋友、顾问），但要明确这是AI")

        return "\n".join(sections)

    @staticmethod
    def _energy_label(affect: AffectState) -> str:
        if affect.energy > 0.7:
            return "精力充沛"
        elif affect.energy > 0.4:
            return "正常"
        elif affect.energy > 0.2:
            return "有点疲惫"
        else:
            return "非常疲惫"

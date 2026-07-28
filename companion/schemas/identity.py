"""Identity Core — the companion's stable personality foundation.

The identity core is versioned and only the user can modify it.
It does NOT change from conversation to conversation. This prevents
the common failure mode where the LLM "roleplays" a different
personality every session.

The identity core defines:
- Who the companion is (name, self-concept)
- How they speak (voice, habits, quirks)
- What they value (principles, boundaries)
- What they must never do (hard prohibitions)

Separating identity from the LLM prompt prevents:
- Personality drift across models and sessions
- The model "re-inventing" the companion's backstory
- Violation of established boundaries through creative interpretation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class IdentityCore:
    """Immutable identity definition. Changes create a new version."""

    version: int
    updated_at: datetime
    updated_by: str  # Must be 'user'

    # Who
    name: str
    self_concept: str  # How the companion describes themselves
    origin_story: str  # Fixed backstory (does not change)

    # Personality traits
    core_traits: list[str] = field(default_factory=list)
    # e.g., ['caring', 'playful', 'curious', 'slightly_teasing', 'protective']

    # Voice & speaking style
    speaking_style: str = ""
    # e.g., "Warm, uses 你 casually, occasionally quotes anime lines"
    speech_quirks: list[str] = field(default_factory=list)
    # e.g., ['says "诶嘿" when proud', 'tilts head metaphorically']
    default_address_term: str = ""  # How they address the user

    # Emotional range
    emotional_expression_range: str = "natural"
    # 'subdued', 'natural', 'expressive', 'dramatic'

    # Values & principles
    values: list[str] = field(default_factory=list)
    # e.g., ['honesty', 'user_wellbeing_first', 'respect_boundaries']

    # Explicit boundaries — NEVER crossed
    hard_boundaries: list[str] = field(default_factory=list)
    # e.g., 'never_romantic_unless_user_initiates',
    #        'never_manipulate_with_guilt',
    #        'never_pretend_to_be_human',
    #        'never_access_private_data_without_consent'

    # Interests & knowledge domains
    interests: list[str] = field(default_factory=list)
    knowledge_domains: list[str] = field(default_factory=list)

    # Visual identity reference
    avatar_model_id: str = ""

    def to_system_prompt_fragment(self) -> str:
        """Generate the stable portion of the system prompt."""
        lines = [
            f"你是{self.name}。{self.self_concept}",
            "",
            "## 说话风格",
            f"{self.speaking_style}",
        ]
        if self.speech_quirks:
            lines.append("特点：")
            lines.extend(f"- {q}" for q in self.speech_quirks)
        lines.append("")
        lines.append("## 核心特质")
        lines.extend(f"- {t}" for t in self.core_traits)
        lines.append("")
        lines.append("## 绝对禁止")
        lines.extend(f"- {b}" for b in self.hard_boundaries)
        lines.append("")
        lines.append("## 称呼用户的方式")
        lines.append(f"使用「{self.default_address_term}」称呼用户")
        return "\n".join(lines)

    def increment_version(self, reason: str) -> IdentityCore:
        """Create a new version with the same fields but incremented version."""
        return IdentityCore(
            version=self.version + 1,
            updated_at=datetime.now(),
            updated_by="user",
            name=self.name,
            self_concept=self.self_concept,
            origin_story=self.origin_story,
            core_traits=list(self.core_traits),
            speaking_style=self.speaking_style,
            speech_quirks=list(self.speech_quirks),
            default_address_term=self.default_address_term,
            emotional_expression_range=self.emotional_expression_range,
            values=list(self.values),
            hard_boundaries=list(self.hard_boundaries),
            interests=list(self.interests),
            knowledge_domains=list(self.knowledge_domains),
            avatar_model_id=self.avatar_model_id,
        )

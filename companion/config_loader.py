"""Config Loader — reads default.yaml and builds runtime components.

Maps the YAML configuration file into provider configs, identity
definitions, and policy parameters that can be injected into the
orchestrator and all providers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from companion.providers.implementations.cloud_llm import CloudLLMConfig
from companion.providers.implementations.cloud_tts import CloudTTSConfig
from companion.providers.implementations.faster_whisper_asr import FasterWhisperConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionConfig,
)
from companion.schemas.identity import IdentityCore
from companion.services.action_service import ActionServiceConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "resources" / "default.yaml"


@dataclass
class RuntimeConfig:
    """Complete runtime configuration assembled from YAML + env vars."""

    # Identity
    identity: IdentityCore | None = None

    # Providers
    llm_config: CloudLLMConfig | None = None
    tts_config: CloudTTSConfig | None = None
    asr_config: FasterWhisperConfig | None = None
    avatar_config: WebSocketAvatarConfig | None = None
    action_provider_config: WindowsReadOnlyActionConfig | None = None
    action_service_config: ActionServiceConfig | None = None
    action_audit_db_path: str = ""

    # Policy
    quiet_hours_enabled: bool = True
    quiet_hours_start: int = 23
    quiet_hours_end: int = 7
    proactive_level_1_per_hour: int = 30
    proactive_level_2_per_hour: int = 10
    proactive_level_3_per_hour: int = 3
    proactive_level_4_per_hour: int = 1

    # Dev
    log_level: str = "INFO"
    log_file: str = ""

    # Raw config for inspection
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path | str | None = None) -> RuntimeConfig:
        """Load and parse the YAML configuration file.

        Environment variables override YAML values where applicable
        (e.g. ANTHROPIC_API_KEY overrides the api_key_env field).
        """
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            logger.warning("Config file not found at %s, using defaults", config_path)
            return cls()

        with open(config_path, encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        cfg = cls(raw=raw)

        # ── Identity ──────────────────────────────────────────────────
        identity_raw = raw.get("identity", {})
        cfg.identity = IdentityCore(
            version=1,
            updated_at=datetime.now(UTC),
            updated_by="user",
            name=identity_raw.get("name", "未命名伙伴"),
            self_concept=identity_raw.get("self_concept", ""),
            origin_story=identity_raw.get("origin_story", ""),
            core_traits=identity_raw.get("core_traits", []),
            speaking_style=identity_raw.get("speaking_style", ""),
            speech_quirks=identity_raw.get("speech_quirks", []),
            default_address_term=identity_raw.get("default_address_term", "你"),
            emotional_expression_range=identity_raw.get("emotional_expression_range", "natural"),
            values=identity_raw.get("values", []),
            hard_boundaries=identity_raw.get("hard_boundaries", []),
            interests=identity_raw.get("interests", []),
            knowledge_domains=identity_raw.get("knowledge_domains", []),
            avatar_model_id=identity_raw.get("avatar_model_id", ""),
        )

        # ── LLM Provider ──────────────────────────────────────────────
        providers_raw = raw.get("providers", {})
        llm_raw = providers_raw.get("llm", {})
        llm_cloud = llm_raw.get("cloud", {})
        cfg.llm_config = CloudLLMConfig(
            provider=llm_cloud.get("provider", "anthropic"),
            model=llm_cloud.get("model", "claude-sonnet-5"),
            api_key=llm_cloud.get("api_key", ""),
            api_key_env=llm_cloud.get("api_key_env", ""),
            api_key_file=llm_cloud.get("api_key_file", ""),
            base_url=llm_cloud.get("base_url", ""),
            max_retries=3,
            timeout_seconds=30.0,
        )

        # ── TTS Provider ──────────────────────────────────────────────
        tts_raw = providers_raw.get("tts", {})
        tts_cloud = tts_raw.get("providers", {}).get("cloud", {})
        cfg.tts_config = CloudTTSConfig(
            provider=tts_cloud.get("provider", "azure"),
            voice=tts_cloud.get("voice", "zh-CN-XiaoxiaoNeural"),
            api_key_env=tts_cloud.get("api_key_env", "AZURE_SPEECH_KEY")
            if isinstance(tts_cloud, dict)
            else "AZURE_SPEECH_KEY",
            region="eastasia",
            sample_rate=tts_raw.get("sample_rate", 24000),
        )

        # ── ASR Provider ──────────────────────────────────────────────
        asr_raw = providers_raw.get("asr", {})
        asr_batch = asr_raw.get("batch", {})
        if asr_batch.get("provider", "faster-whisper") == "faster-whisper":
            cfg.asr_config = FasterWhisperConfig(
                model_size=asr_batch.get("model", "base"),
                device=asr_batch.get("device", "auto"),
                compute_type=asr_batch.get("compute_type", "default"),
                cpu_threads=int(asr_batch.get("cpu_threads", 0)),
            )

        # ── Avatar bridge ────────────────────────────────────────────
        avatar_raw = providers_raw.get("avatar", {})
        if avatar_raw.get("enabled", False):
            if avatar_raw.get("type") != "websocket_bridge":
                raise ValueError("enabled avatar provider type must be 'websocket_bridge'")
            cfg.avatar_config = WebSocketAvatarConfig(
                url=avatar_raw.get("url", "ws://127.0.0.1:6121/ws"),
                auth_token_env=avatar_raw.get("auth_token_env", "COMPANION_AVATAR_TOKEN"),
                connect_timeout_seconds=float(
                    avatar_raw.get("connect_timeout_seconds", 3.0)
                ),
                request_timeout_seconds=float(
                    avatar_raw.get("request_timeout_seconds", 3.0)
                ),
                max_message_bytes=int(avatar_raw.get("max_message_bytes", 1_048_576)),
            )

        # ── Windows read-only actions ─────────────────────────────────
        action_raw = providers_raw.get("action", {})
        if action_raw.get("enabled", False):
            if action_raw.get("type") != "windows_readonly":
                raise ValueError("enabled action provider type must be 'windows_readonly'")
            if not action_raw.get("sandbox_enabled", True):
                raise ValueError("enabled actions require sandbox_enabled")
            audit_db_path = str(action_raw.get("audit_db_path", "")).strip()
            if not audit_db_path:
                raise ValueError("enabled actions require a durable audit_db_path")
            cfg.action_provider_config = WindowsReadOnlyActionConfig(
                max_text_characters=int(action_raw.get("max_text_characters", 4096))
            )
            cfg.action_service_config = ActionServiceConfig(
                sandbox_enabled=True,
                max_concurrent_actions=int(action_raw.get("max_concurrent_actions", 1)),
                action_timeout_seconds=float(action_raw.get("timeout_seconds", 5.0)),
                undo_enabled=False,
                audit_enabled=True,
                require_durable_audit=True,
                allow_readonly_auto=True,
                allow_reversible_low_auto=False,
                allow_reversible_high_auto=False,
                allow_irreversible_auto=False,
            )
            cfg.action_audit_db_path = audit_db_path

        # ── Policy ────────────────────────────────────────────────────
        policy_raw = raw.get("policy", {})
        quiet = policy_raw.get("quiet_hours", {})
        cfg.quiet_hours_enabled = quiet.get("enabled", True)
        cfg.quiet_hours_start = _parse_hour(quiet.get("start", "23:00"))
        cfg.quiet_hours_end = _parse_hour(quiet.get("end", "07:00"))
        budget = policy_raw.get("proactive_budget", {})
        cfg.proactive_level_1_per_hour = budget.get("level_1_per_hour", 30)
        cfg.proactive_level_2_per_hour = budget.get("level_2_per_hour", 10)
        cfg.proactive_level_3_per_hour = budget.get("level_3_per_hour", 3)
        cfg.proactive_level_4_per_hour = budget.get("level_4_per_hour", 1)

        # ── Dev ───────────────────────────────────────────────────────
        dev_raw = raw.get("dev", {})
        cfg.log_level = dev_raw.get("log_level", "INFO")
        cfg.log_file = dev_raw.get("log_file", "")

        return cfg


def _parse_hour(hhmm: str) -> int:
    """Parse 'HH:MM' to int hour."""
    try:
        return int(hhmm.split(":")[0])
    except (ValueError, IndexError):
        return 0

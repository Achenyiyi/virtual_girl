"""Config Loader — reads default.yaml and builds runtime components.

Maps the YAML configuration file into provider configs, identity
definitions, and policy parameters that can be injected into the
orchestrator and all providers.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from companion.audio.microphone import MicConfig
from companion.core.policy_gate import PolicyGateConfig
from companion.memory.memory_service import MemoryServiceConfig
from companion.providers.implementations.cloud_llm import CloudLLMConfig
from companion.providers.implementations.cloud_tts import CloudTTSConfig
from companion.providers.implementations.faster_whisper_asr import FasterWhisperConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig
from companion.providers.implementations.windows_readonly_action import (
    WindowsReadOnlyActionConfig,
)
from companion.schemas.identity import IdentityCore
from companion.services.action_service import ActionServiceConfig
from companion.services.avatar_stage_supervisor import AvatarStageLaunchConfig
from companion.services.voice_pipeline import VoicePipelineConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "resources" / "default.yaml"

type ConfigSchema = dict[str, ConfigSchema | None]


def _fields(*names: str) -> ConfigSchema:
    return dict.fromkeys(names)


_CONFIG_SCHEMA: ConfigSchema = {
    "runtime": _fields("data_root"),
    "identity": _fields(
        "name",
        "self_concept",
        "origin_story",
        "core_traits",
        "speaking_style",
        "speech_quirks",
        "default_address_term",
        "emotional_expression_range",
        "values",
        "hard_boundaries",
        "interests",
        "knowledge_domains",
        "avatar_model_id",
    ),
    "providers": {
        "llm": {
            "type": None,
            "cloud": _fields(
                "provider",
                "model",
                "api_key",
                "api_key_file",
                "api_key_env",
                "credential_target",
                "base_url",
                "max_retries",
                "retry_delay_seconds",
                "timeout_seconds",
            ),
        },
        "tts": {
            "type": None,
            "sample_rate": None,
            "providers": {
                "cloud": _fields(
                    "enabled",
                    "provider",
                    "voice",
                    "api_key_env",
                    "credential_target",
                    "region",
                    "timeout_seconds",
                )
            },
        },
        "asr": {
            "capture": _fields(
                "language",
                "sample_rate",
                "pre_roll_ms",
                "max_speech_duration_ms",
                "silence_duration_ms",
                "max_turn_duration_ms",
                "tts_chunk_timeout_seconds",
                "playback_timeout_seconds",
                "cleanup_timeout_seconds",
                "interrupt_timeout_seconds",
                "target_e2e_latency_ms",
                "target_interrupt_latency_ms",
            ),
            "batch": _fields("provider", "model", "device", "compute_type", "cpu_threads"),
        },
        "memory": _fields("type", "db_path", "wal_mode", "fts_enabled"),
        "avatar": {
            "enabled": None,
            "type": None,
            "url": None,
            "auth_token_env": None,
            "credential_target": None,
            "connect_timeout_seconds": None,
            "request_timeout_seconds": None,
            "max_message_bytes": None,
            "launch": _fields(
                "enabled",
                "executable_path",
                "expected_sha256",
                "expected_app_asar_sha256",
                "expected_godot_sha256",
                "model_path",
                "expected_model_sha256",
                "model_id",
                "model_name",
                "startup_timeout_seconds",
                "shutdown_timeout_seconds",
            ),
        },
        "action": _fields(
            "enabled",
            "type",
            "sandbox_enabled",
            "audit_db_path",
            "timeout_seconds",
            "max_concurrent_actions",
            "max_pending_confirmations",
            "confirmation_ttl_seconds",
            "max_text_characters",
        ),
        "perception": {"enabled": None},
    },
    "policy": {
        "quiet_hours": _fields("enabled", "start", "end"),
        "proactive_budget": _fields(*(f"level_{level}_per_hour" for level in range(1, 5))),
        "cooldown_seconds": _fields(*(f"level_{level}" for level in range(1, 5))),
    },
    "telemetry": {"enabled": None},
    "dev": _fields(
        "log_level",
        "log_file",
        "log_max_bytes",
        "log_backup_count",
        "event_log_retention",
    ),
}


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
    avatar_stage_launch_config: AvatarStageLaunchConfig | None = None
    action_provider_config: WindowsReadOnlyActionConfig | None = None
    action_service_config: ActionServiceConfig | None = None
    action_audit_db_path: str = ""
    memory_config: MemoryServiceConfig | None = None
    microphone_config: MicConfig = field(default_factory=MicConfig)
    voice_pipeline_config: VoicePipelineConfig = field(default_factory=VoicePipelineConfig)

    # Policy
    policy_config: PolicyGateConfig = field(default_factory=PolicyGateConfig)

    # Dev
    log_level: str = "INFO"
    log_file: str = ""
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    event_log_retention: int = 10_000

    # Raw config for inspection
    raw: dict[str, Any] = field(default_factory=dict)

    def effective_memory_config(self) -> MemoryServiceConfig:
        """Return the configured memory settings with the documented env override."""
        config = self.memory_config or MemoryServiceConfig(
            db_path="./data/companion_memory.db"
        )
        override = os.environ.get("COMPANION_DB_PATH", "").strip()
        if not override:
            return config
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError("COMPANION_DB_PATH must be an absolute path")
        return replace(config, db_path=str(candidate.resolve()))

    @classmethod
    def from_yaml(cls, path: Path | str | None = None) -> RuntimeConfig:
        """Load and parse the YAML configuration file.

        Environment variables override YAML values where applicable
        (e.g. ANTHROPIC_API_KEY overrides the api_key_env field).
        """
        explicit_path = path is not None
        config_path = (
            Path(path).expanduser().resolve()
            if path is not None
            else DEFAULT_CONFIG_PATH
        )
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {config_path}")

        try:
            with open(config_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML configuration: {config_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("Configuration root must be a YAML mapping")
        raw: dict[str, Any] = loaded
        _validate_known_fields(raw, _CONFIG_SCHEMA)
        runtime_raw = _section(raw, "runtime")
        runtime_root = _runtime_root(
            config_path,
            explicit=explicit_path,
            mode=str(runtime_raw.get("data_root", "auto")),
        )
        asset_root = config_path.parent

        cfg = cls(raw=raw)

        # ── Identity ──────────────────────────────────────────────────
        identity_raw = _section(raw, "identity")
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
        providers_raw = _section(raw, "providers")
        llm_raw = _section(providers_raw, "llm")
        if llm_raw.get("type", "cloud") != "cloud":
            raise ValueError("only the cloud LLM provider is currently implemented")
        llm_cloud = _section(llm_raw, "cloud")
        if llm_cloud.get("api_key") or llm_cloud.get("api_key_file"):
            raise ValueError(
                "YAML and file credentials are forbidden; configure api_key_env or "
                "credential_target instead"
            )
        cfg.llm_config = CloudLLMConfig(
            provider=llm_cloud.get("provider", "anthropic"),
            model=llm_cloud.get("model", "claude-sonnet-5"),
            api_key=llm_cloud.get("api_key", ""),
            api_key_env=llm_cloud.get("api_key_env", ""),
            credential_target=llm_cloud.get("credential_target", ""),
            base_url=llm_cloud.get("base_url", ""),
            max_retries=int(llm_cloud.get("max_retries", 3)),
            retry_delay_seconds=float(llm_cloud.get("retry_delay_seconds", 1.0)),
            timeout_seconds=float(llm_cloud.get("timeout_seconds", 30.0)),
        )

        # ── TTS Provider ──────────────────────────────────────────────
        tts_raw = _section(providers_raw, "tts")
        if tts_raw.get("type", "cloud") != "cloud":
            raise ValueError("only the cloud TTS provider is currently implemented")
        tts_providers = _section(tts_raw, "providers")
        tts_cloud = _section(tts_providers, "cloud")
        if _boolean(tts_cloud.get("enabled", True), "providers.tts.providers.cloud.enabled"):
            cfg.tts_config = CloudTTSConfig(
                provider=tts_cloud.get("provider", "azure"),
                voice=tts_cloud.get("voice", "zh-CN-XiaoxiaoNeural"),
                api_key_env=tts_cloud.get("api_key_env", "AZURE_SPEECH_KEY"),
                credential_target=tts_cloud.get("credential_target", ""),
                region=tts_cloud.get("region", "eastasia"),
                sample_rate=int(tts_raw.get("sample_rate", 24000)),
                timeout_seconds=float(tts_cloud.get("timeout_seconds", 15.0)),
            )

        # ── ASR Provider ──────────────────────────────────────────────
        asr_raw = _section(providers_raw, "asr")
        capture_raw = _section(asr_raw, "capture")
        asr_batch = _section(asr_raw, "batch")
        if asr_batch.get("provider", "faster-whisper") == "faster-whisper":
            cfg.asr_config = FasterWhisperConfig(
                model_size=asr_batch.get("model", "base"),
                device=asr_batch.get("device", "auto"),
                compute_type=asr_batch.get("compute_type", "default"),
                cpu_threads=int(asr_batch.get("cpu_threads", 0)),
            )
        elif asr_batch:
            raise ValueError("only faster-whisper batch ASR is currently implemented")
        voice_sample_rate = int(capture_raw.get("sample_rate", 16000))
        voice_language = str(capture_raw.get("language", "zh"))
        pre_roll_ms = int(capture_raw.get("pre_roll_ms", 400))
        cfg.microphone_config = MicConfig(
            sample_rate=voice_sample_rate,
            pre_roll_buffer_ms=pre_roll_ms,
            max_speech_duration_ms=int(capture_raw.get("max_speech_duration_ms", 30_000)),
            silence_duration_ms=int(capture_raw.get("silence_duration_ms", 800)),
        )
        cfg.voice_pipeline_config = VoicePipelineConfig(
            sample_rate=voice_sample_rate,
            language=voice_language,
            pre_roll_ms=pre_roll_ms,
            max_turn_duration_ms=int(capture_raw.get("max_turn_duration_ms", 30_000)),
            tts_chunk_timeout_seconds=float(
                capture_raw.get("tts_chunk_timeout_seconds", 15.0)
            ),
            playback_timeout_seconds=float(
                capture_raw.get("playback_timeout_seconds", 30.0)
            ),
            cleanup_timeout_seconds=float(capture_raw.get("cleanup_timeout_seconds", 2.0)),
            interrupt_timeout_seconds=float(
                capture_raw.get("interrupt_timeout_seconds", 0.3)
            ),
            target_e2e_latency_ms=int(capture_raw.get("target_e2e_latency_ms", 900)),
            target_interrupt_latency_ms=int(
                capture_raw.get("target_interrupt_latency_ms", 300)
            ),
        )

        # ── Memory ────────────────────────────────────────────────────
        memory_raw = _section(providers_raw, "memory")
        if memory_raw.get("type", "sqlite") != "sqlite":
            raise ValueError("only SQLite memory is currently implemented")
        cfg.memory_config = MemoryServiceConfig(
            db_path=_resolve_runtime_path(
                memory_raw.get("db_path", "./data/companion_memory.db"), runtime_root
            ),
            wal_mode=_boolean(memory_raw.get("wal_mode", True), "providers.memory.wal_mode"),
            fts_enabled=_boolean(
                memory_raw.get("fts_enabled", True), "providers.memory.fts_enabled"
            ),
        )

        # ── Avatar bridge ────────────────────────────────────────────
        avatar_raw = _section(providers_raw, "avatar")
        avatar_enabled = _boolean(
            avatar_raw.get("enabled", False), "providers.avatar.enabled"
        )
        if avatar_enabled:
            if avatar_raw.get("type") != "websocket_bridge":
                raise ValueError("enabled avatar provider type must be 'websocket_bridge'")
            cfg.avatar_config = WebSocketAvatarConfig(
                url=avatar_raw.get("url", "ws://127.0.0.1:6122/ws"),
                auth_token_env=avatar_raw.get("auth_token_env", "COMPANION_AVATAR_TOKEN"),
                credential_target=avatar_raw.get("credential_target", ""),
                connect_timeout_seconds=float(
                    avatar_raw.get("connect_timeout_seconds", 3.0)
                ),
                request_timeout_seconds=float(
                    avatar_raw.get("request_timeout_seconds", 3.0)
                ),
                max_message_bytes=int(avatar_raw.get("max_message_bytes", 1_048_576)),
            )
        launch_raw = _section(avatar_raw, "launch")
        if _boolean(
            launch_raw.get("enabled", False), "providers.avatar.launch.enabled"
        ):
            if not avatar_enabled or cfg.avatar_config is None:
                raise ValueError("managed avatar launch requires the avatar provider")
            if cfg.avatar_config.url != "ws://127.0.0.1:6122/ws":
                raise ValueError(
                    "managed avatar launch requires url 'ws://127.0.0.1:6122/ws'"
                )
            if cfg.avatar_config.auth_token_env != "COMPANION_AVATAR_TOKEN":
                raise ValueError(
                    "managed avatar launch requires auth_token_env "
                    "'COMPANION_AVATAR_TOKEN'"
                )
            executable_path = str(launch_raw.get("executable_path", "")).strip()
            if not executable_path:
                raise ValueError(
                    "managed avatar launch executable_path must not be empty"
                )
            model_path = str(launch_raw.get("model_path", "")).strip()
            if not model_path:
                raise ValueError("managed avatar launch model_path must not be empty")
            model_id = str(launch_raw.get("model_id", "")).strip()
            if not model_id:
                raise ValueError("managed avatar launch model_id must not be empty")
            if cfg.identity is None or cfg.identity.avatar_model_id != model_id:
                raise ValueError(
                    "managed avatar launch model_id must match identity.avatar_model_id"
                )
            cfg.avatar_stage_launch_config = AvatarStageLaunchConfig(
                executable_path=_resolve_runtime_path(
                    executable_path, asset_root
                ),
                expected_sha256=str(launch_raw.get("expected_sha256", "")),
                expected_app_asar_sha256=str(
                    launch_raw.get("expected_app_asar_sha256", "")
                ),
                expected_godot_sha256=str(
                    launch_raw.get("expected_godot_sha256", "")
                ),
                model_path=_resolve_runtime_path(model_path, asset_root),
                expected_model_sha256=str(
                    launch_raw.get("expected_model_sha256", "")
                ),
                model_id=model_id,
                model_name=str(
                    launch_raw.get("model_name", "Managed VRM avatar")
                ),
                startup_timeout_seconds=float(
                    launch_raw.get("startup_timeout_seconds", 30.0)
                ),
                shutdown_timeout_seconds=float(
                    launch_raw.get("shutdown_timeout_seconds", 8.0)
                ),
            )

        # ── Windows read-only actions ─────────────────────────────────
        action_raw = _section(providers_raw, "action")
        if _boolean(action_raw.get("enabled", False), "providers.action.enabled"):
            if action_raw.get("type") != "windows_readonly":
                raise ValueError("enabled action provider type must be 'windows_readonly'")
            if not _boolean(
                action_raw.get("sandbox_enabled", True),
                "providers.action.sandbox_enabled",
            ):
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
                max_pending_confirmations=int(
                    action_raw.get("max_pending_confirmations", 10)
                ),
                confirmation_ttl_seconds=float(
                    action_raw.get("confirmation_ttl_seconds", 120.0)
                ),
                undo_enabled=False,
                audit_enabled=True,
                require_durable_audit=True,
                allow_readonly_auto=True,
                allow_reversible_low_auto=False,
                allow_reversible_high_auto=False,
                allow_irreversible_auto=False,
            )
            cfg.action_audit_db_path = _resolve_runtime_path(audit_db_path, runtime_root)

        # ── Policy ────────────────────────────────────────────────────
        policy_raw = _section(raw, "policy")
        quiet = _section(policy_raw, "quiet_hours")
        budget = _section(policy_raw, "proactive_budget")
        cooldown = _section(policy_raw, "cooldown_seconds")
        cfg.policy_config = PolicyGateConfig(
            quiet_hours_enabled=_boolean(quiet.get("enabled", True), "policy.quiet_hours.enabled"),
            quiet_hours_start_hour=_parse_hour(quiet.get("start", "23:00")),
            quiet_hours_end_hour=_parse_hour(quiet.get("end", "07:00")),
            level_1_per_hour=int(budget.get("level_1_per_hour", 30)),
            level_2_per_hour=int(budget.get("level_2_per_hour", 10)),
            level_3_per_hour=int(budget.get("level_3_per_hour", 3)),
            level_4_per_hour=int(budget.get("level_4_per_hour", 1)),
            level_1_cooldown_seconds=float(cooldown.get("level_1", 5)),
            level_2_cooldown_seconds=float(cooldown.get("level_2", 30)),
            level_3_cooldown_seconds=float(cooldown.get("level_3", 300)),
            level_4_cooldown_seconds=float(cooldown.get("level_4", 1800)),
        )

        perception_raw = _section(providers_raw, "perception")
        if _boolean(perception_raw.get("enabled", False), "providers.perception.enabled"):
            raise ValueError("perception is not implemented and must remain disabled")
        telemetry_raw = _section(raw, "telemetry")
        if _boolean(telemetry_raw.get("enabled", False), "telemetry.enabled"):
            raise ValueError("telemetry export is not implemented and must remain disabled")

        # ── Dev ───────────────────────────────────────────────────────
        dev_raw = _section(raw, "dev")
        cfg.log_level = dev_raw.get("log_level", "INFO")
        log_file = str(dev_raw.get("log_file", "")).strip()
        cfg.log_file = _resolve_runtime_path(log_file, runtime_root) if log_file else ""
        cfg.log_max_bytes = int(dev_raw.get("log_max_bytes", 10 * 1024 * 1024))
        cfg.log_backup_count = int(dev_raw.get("log_backup_count", 5))
        cfg.event_log_retention = int(dev_raw.get("event_log_retention", 10_000))
        if not 1024 <= cfg.log_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("log_max_bytes must be between 1 KiB and 1 GiB")
        if not 1 <= cfg.log_backup_count <= 100:
            raise ValueError("log_backup_count must be between 1 and 100")
        if not 100 <= cfg.event_log_retention <= 100_000:
            raise ValueError("event_log_retention must be between 100 and 100000")

        return cfg


def _parse_hour(hhmm: object) -> int:
    """Parse 'HH:MM' to int hour."""
    if not isinstance(hhmm, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", hhmm):
        raise ValueError("quiet-hours values must use 24-hour HH:MM format")
    return int(hhmm[:2])


def _section(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Configuration section '{key}' must be a mapping")
    return value


def _validate_known_fields(
    value: Mapping[Any, Any], schema: ConfigSchema, path: str = "configuration"
) -> None:
    """Reject typos instead of silently falling back to production defaults."""
    unexpected = sorted(repr(key) for key in value if not isinstance(key, str) or key not in schema)
    if unexpected:
        raise ValueError(f"Unknown configuration field(s) in {path}: {', '.join(unexpected)}")
    for key, nested_schema in schema.items():
        if key not in value or nested_schema is None:
            continue
        nested_value = value[key]
        nested_path = key if path == "configuration" else f"{path}.{key}"
        if not isinstance(nested_value, dict):
            raise ValueError(f"Configuration section '{nested_path}' must be a mapping")
        _validate_known_fields(nested_value, nested_schema, nested_path)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Configuration field '{field_name}' must be true or false")
    return value


def _runtime_root(config_path: Path, *, explicit: bool, mode: str) -> Path:
    """Choose a deterministic writable root for relative runtime data paths."""
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "config_directory", "user_local"}:
        raise ValueError(
            "runtime.data_root must be 'auto', 'config_directory', or 'user_local'"
        )
    override = os.environ.get("COMPANION_RUNTIME_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError("COMPANION_RUNTIME_DIR must be an absolute path")
        return candidate.resolve()
    if normalized_mode == "config_directory":
        if not explicit:
            raise ValueError(
                "runtime.data_root 'config_directory' requires an explicit configuration file"
            )
        return config_path.parent
    if normalized_mode == "auto" and explicit:
        return config_path.parent
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "VirtualCompanion"
    return Path.home() / ".virtual-companion"


def _resolve_runtime_path(value: object, root: Path) -> str:
    raw = str(value).strip()
    if not raw:
        raise ValueError("runtime path must not be empty")
    candidate = Path(raw).expanduser()
    return str(candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve())

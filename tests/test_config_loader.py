"""Configuration loading must work from an installed package in any working directory."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from companion.config_loader import DEFAULT_CONFIG_PATH, RuntimeConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig


def test_packaged_default_config_is_cwd_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("COMPANION_RUNTIME_DIR", str(runtime_root))

    config = RuntimeConfig.from_yaml()

    assert DEFAULT_CONFIG_PATH.is_file()
    assert config.identity is not None
    assert config.identity.name == "未命名伙伴"
    assert config.llm_config is not None
    assert config.llm_config.api_key_env == "DEEPSEEK_API_KEY"
    assert config.llm_config.credential_target == "VirtualCompanion/DeepSeek"
    assert config.llm_config.model == "deepseek-chat"
    assert config.llm_config.max_retries == 3
    assert config.tts_config is not None
    assert config.tts_config.region == "eastasia"
    assert config.tts_config.credential_target == "VirtualCompanion/AzureSpeech"
    assert config.memory_config is not None
    assert config.memory_config.db_path == str(
        (runtime_root / "data" / "companion_memory.db").resolve()
    )
    assert config.log_file == str((runtime_root / "data" / "companion.log").resolve())
    assert config.microphone_config.sample_rate == 16000
    assert config.microphone_config.pre_roll_buffer_ms == 400
    assert config.voice_pipeline_config.language == "zh"
    assert config.voice_pipeline_config.tts_chunk_timeout_seconds == 15.0
    assert config.voice_pipeline_config.playback_timeout_seconds == 30.0
    assert config.voice_pipeline_config.cleanup_timeout_seconds == 2.0
    assert config.voice_pipeline_config.interrupt_timeout_seconds == 0.3
    assert config.voice_pipeline_config.target_e2e_latency_ms == 900
    assert config.voice_pipeline_config.target_interrupt_latency_ms == 300
    assert config.policy_config.level_4_per_hour == 1
    assert config.policy_config.level_4_cooldown_seconds == 1800
    assert config.log_max_bytes == 10 * 1024 * 1024
    assert config.log_backup_count == 5
    assert config.event_log_retention == 10_000


def test_missing_explicit_config_fails_closed(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        RuntimeConfig.from_yaml(tmp_path / "missing.yaml")


def test_voice_pipeline_timeouts_are_configurable(tmp_path) -> None:
    path = tmp_path / "voice-timeouts.yaml"
    path.write_text(
        """providers:
  asr:
    capture:
      tts_chunk_timeout_seconds: 1.1
      playback_timeout_seconds: 2.2
      cleanup_timeout_seconds: 3.3
      interrupt_timeout_seconds: 0.4
      target_e2e_latency_ms: 1200
      target_interrupt_latency_ms: 250
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path).voice_pipeline_config

    assert config.tts_chunk_timeout_seconds == 1.1
    assert config.playback_timeout_seconds == 2.2
    assert config.cleanup_timeout_seconds == 3.3
    assert config.interrupt_timeout_seconds == 0.4
    assert config.target_e2e_latency_ms == 1200
    assert config.target_interrupt_latency_ms == 250


@pytest.mark.parametrize(
    "field",
    ["target_e2e_latency_ms", "target_interrupt_latency_ms"],
)
def test_voice_acceptance_latency_targets_must_be_positive(tmp_path, field) -> None:
    path = tmp_path / "invalid-voice-target.yaml"
    path.write_text(
        f"providers:\n  asr:\n    capture:\n      {field}: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        RuntimeConfig.from_yaml(path)


def test_memory_environment_override_is_applied_at_runtime(tmp_path, monkeypatch) -> None:
    override = tmp_path / "override.db"
    monkeypatch.setenv("COMPANION_DB_PATH", str(override))

    config = RuntimeConfig.from_yaml()

    assert config.effective_memory_config().db_path == str(override)


def test_repository_config_template_matches_packaged_default() -> None:
    repository_default = Path(__file__).parents[1] / "config" / "default.yaml"

    assert yaml.safe_load(repository_default.read_text(encoding="utf-8")) == yaml.safe_load(
        DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    )


def test_avatar_bridge_is_disabled_by_default() -> None:
    config = RuntimeConfig.from_yaml()

    assert config.avatar_config is None


def test_enabled_avatar_bridge_config_is_parsed(tmp_path) -> None:
    path = tmp_path / "avatar.yaml"
    path.write_text(
        """providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:9999/avatar
    auth_token_env: TEST_AVATAR_TOKEN
    request_timeout_seconds: 1.5
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path)

    assert config.avatar_config is not None
    assert config.avatar_config.url == "ws://127.0.0.1:9999/avatar"
    assert config.avatar_config.auth_token_env == "TEST_AVATAR_TOKEN"
    assert config.avatar_config.credential_target == ""
    assert config.avatar_config.request_timeout_seconds == 1.5


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.com/avatar",
        "ws://user:secret@127.0.0.1/avatar",
        "ws://127.0.0.1/avatar?token=secret",
    ],
)
def test_avatar_bridge_rejects_unsafe_urls(url) -> None:
    with pytest.raises(ValueError):
        WebSocketAvatarConfig(url=url)


def test_windows_actions_are_disabled_by_default() -> None:
    config = RuntimeConfig.from_yaml()

    assert config.action_provider_config is None
    assert config.action_service_config is None
    assert config.action_audit_db_path == ""


def test_enabled_windows_readonly_actions_require_safe_boundary(tmp_path) -> None:
    path = tmp_path / "actions.yaml"
    audit_path = tmp_path / "audit.db"
    path.write_text(
        f"""providers:
  action:
    enabled: true
    type: windows_readonly
    sandbox_enabled: true
    audit_db_path: {audit_path.as_posix()}
    timeout_seconds: 2.5
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path)

    assert config.action_provider_config is not None
    assert config.action_service_config is not None
    assert config.action_service_config.sandbox_enabled
    assert config.action_service_config.require_durable_audit
    assert not config.action_service_config.allow_reversible_low_auto
    assert config.action_service_config.max_pending_confirmations == 10
    assert config.action_service_config.confirmation_ttl_seconds == 120.0
    assert config.action_audit_db_path == str(audit_path.resolve())


def test_explicit_config_relative_data_paths_resolve_from_config_directory(tmp_path) -> None:
    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    path = config_dir / "companion.yaml"
    path.write_text(
        """providers:
  memory:
    type: sqlite
    db_path: data/memory.db
  action:
    enabled: true
    type: windows_readonly
    sandbox_enabled: true
    audit_db_path: data/audit.db
dev:
  log_file: logs/companion.log
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path)

    assert config.memory_config is not None
    assert config.memory_config.db_path == str((config_dir / "data/memory.db").resolve())
    assert config.action_audit_db_path == str((config_dir / "data/audit.db").resolve())
    assert config.log_file == str((config_dir / "logs/companion.log").resolve())


@pytest.mark.parametrize(
    "action_yaml",
    [
        "type: windows_readonly\nsandbox_enabled: false\naudit_db_path: audit.db",
        "type: windows_readonly\nsandbox_enabled: true\naudit_db_path: ''",
        "type: arbitrary_shell\nsandbox_enabled: true\naudit_db_path: audit.db",
    ],
)
def test_enabled_actions_reject_unsafe_configuration(tmp_path, action_yaml) -> None:
    indented = action_yaml.replace("\n", "\n    ")
    path = tmp_path / "unsafe-actions.yaml"
    path.write_text(
        f"providers:\n  action:\n    enabled: true\n    {indented}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        RuntimeConfig.from_yaml(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"providers": {"llm": {"type": "local"}}},
        {"providers": {"llm": {"type": "cloud", "cloud": {"api_key": "forbidden"}}}},
        {"providers": {"llm": {"type": "cloud", "cloud": {"api_key_file": "key.txt"}}}},
        {"providers": {"asr": {"batch": {"provider": "unknown"}}}},
        {"providers": {"perception": {"enabled": True}}},
        {"telemetry": {"enabled": True}},
    ],
)
def test_unimplemented_or_unsafe_configuration_fails_closed(tmp_path, payload) -> None:
    path = tmp_path / "unsupported.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        RuntimeConfig.from_yaml(path)


def test_non_mapping_yaml_is_rejected(tmp_path) -> None:
    path = tmp_path / "invalid-root.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        RuntimeConfig.from_yaml(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"providers": []},
        {"providers": {"avatar": {"enabled": "false"}}},
        {"policy": {"quiet_hours": {"start": "25:00"}}},
    ],
)
def test_ambiguous_nested_configuration_is_rejected(tmp_path, payload) -> None:
    path = tmp_path / "ambiguous.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        RuntimeConfig.from_yaml(path)


@pytest.mark.parametrize(
    "dev",
    [
        {"log_max_bytes": 0},
        {"log_backup_count": 0},
        {"event_log_retention": 99},
        {"event_log_retention": 100_001},
    ],
)
def test_unbounded_or_invalid_resource_configuration_is_rejected(tmp_path, dev) -> None:
    path = tmp_path / "invalid-resources.yaml"
    path.write_text(yaml.safe_dump({"dev": dev}), encoding="utf-8")

    with pytest.raises(ValueError):
        RuntimeConfig.from_yaml(path)

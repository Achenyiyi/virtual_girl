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
    assert config.tts_config.provider == "fish_audio"
    assert config.tts_config.model == "s2.1-pro-free"
    assert config.tts_config.reference_id == "7f92f8afb8ec43bf81429cc1c9199cb1"
    assert config.tts_config.api_key_env == "FISH_API_KEY"
    assert config.tts_config.credential_target == "VirtualCompanion/FishAudio"
    assert config.tts_config.latency == "balanced"
    assert config.tts_config.chunk_length == 180
    assert config.tts_config.min_chunk_length == 30
    assert config.tts_config.max_text_bytes == 480
    assert config.memory_config is not None
    assert config.memory_config.db_path == str(
        (runtime_root / "data" / "companion_memory.db").resolve()
    )
    assert config.log_file == str((runtime_root / "data" / "companion.log").resolve())
    assert config.microphone_config.sample_rate == 16000
    assert config.microphone_config.pre_roll_buffer_ms == 400
    assert config.voice_pipeline_config.language == "zh"
    assert config.voice_pipeline_config.tts_chunk_timeout_seconds == 15.0
    assert config.voice_pipeline_config.playback_timeout_seconds == 120.0
    assert config.voice_pipeline_config.max_turn_duration_ms == 120_000
    assert config.voice_pipeline_config.target_e2e_latency_ms == 30_000
    assert config.voice_pipeline_config.cleanup_timeout_seconds == 2.0
    assert config.voice_pipeline_config.interrupt_timeout_seconds == 0.3
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


def test_relative_memory_environment_override_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_DB_PATH", "relative-memory.db")

    config = RuntimeConfig.from_yaml()

    with pytest.raises(ValueError, match="COMPANION_DB_PATH must be an absolute path"):
        config.effective_memory_config()


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


def test_managed_avatar_launch_is_parsed_relative_to_explicit_config(tmp_path) -> None:
    path = tmp_path / "avatar.yaml"
    path.write_text(
        """identity:
  avatar_model_id: managed-nemesia
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: stage/airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: model/nemesia.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia
      model_name: Nemesia pajamas
      startup_timeout_seconds: 12
      shutdown_timeout_seconds: 4
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path)

    assert config.avatar_stage_launch_config is not None
    assert config.avatar_stage_launch_config.executable_path == str(
        (tmp_path / "stage" / "airi.exe").resolve()
    )
    assert config.avatar_stage_launch_config.expected_sha256 == "a" * 64
    assert config.avatar_stage_launch_config.expected_app_asar_sha256 == "b" * 64
    assert config.avatar_stage_launch_config.expected_godot_sha256 == "d" * 64
    assert config.avatar_stage_launch_config.model_path == str(
        (tmp_path / "model" / "nemesia.vrm").resolve()
    )
    assert config.avatar_stage_launch_config.expected_model_sha256 == "c" * 64
    assert config.avatar_stage_launch_config.model_id == "managed-nemesia"
    assert config.avatar_stage_launch_config.model_name == "Nemesia pajamas"
    assert config.avatar_stage_launch_config.startup_timeout_seconds == 12
    assert config.avatar_stage_launch_config.shutdown_timeout_seconds == 4


def test_default_avatar_bridge_uses_dedicated_airi_port() -> None:
    assert WebSocketAvatarConfig().url == "ws://127.0.0.1:6122/ws"


@pytest.mark.parametrize(
    "avatar_yaml",
    [
        """enabled: false
    launch:
      enabled: true
      executable_path: airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: avatar.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia""",
        """enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:9999/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: avatar.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia""",
        """enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: OTHER_TOKEN
    launch:
      enabled: true
      executable_path: airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: avatar.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia""",
    ],
)
def test_managed_avatar_launch_rejects_unsafe_boundary(tmp_path, avatar_yaml) -> None:
    path = tmp_path / "avatar.yaml"
    path.write_text(f"providers:\n  avatar:\n    {avatar_yaml}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="managed avatar launch"):
        RuntimeConfig.from_yaml(path)


def test_managed_avatar_launch_requires_identity_model_match(tmp_path) -> None:
    path = tmp_path / "avatar.yaml"
    path.write_text(
        """identity:
  avatar_model_id: another-model
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: avatar.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must match identity.avatar_model_id"):
        RuntimeConfig.from_yaml(path)


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


def test_user_local_data_root_keeps_managed_assets_relative_to_config(
    tmp_path, monkeypatch
) -> None:
    config_dir = tmp_path / "read-only-installation"
    local_app_data = tmp_path / "local-app-data"
    config_dir.mkdir()
    monkeypatch.delenv("COMPANION_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    path = config_dir / "production.yaml"
    path.write_text(
        """runtime:
  data_root: user_local
identity:
  avatar_model_id: managed-nemesia
providers:
  memory:
    type: sqlite
    db_path: data/memory.db
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: stage/airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: assets/nemesia.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia
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

    user_root = local_app_data / "VirtualCompanion"
    assert config.memory_config is not None
    assert config.memory_config.db_path == str((user_root / "data/memory.db").resolve())
    assert config.action_audit_db_path == str((user_root / "data/audit.db").resolve())
    assert config.log_file == str((user_root / "logs/companion.log").resolve())
    assert config.avatar_stage_launch_config is not None
    assert config.avatar_stage_launch_config.executable_path == str(
        (config_dir / "stage/airi.exe").resolve()
    )
    assert config.avatar_stage_launch_config.model_path == str(
        (config_dir / "assets/nemesia.vrm").resolve()
    )


def test_runtime_directory_override_applies_to_explicit_data_only(
    tmp_path, monkeypatch
) -> None:
    config_dir = tmp_path / "installation"
    data_root = tmp_path / "profile"
    config_dir.mkdir()
    monkeypatch.setenv("COMPANION_RUNTIME_DIR", str(data_root.resolve()))
    path = config_dir / "production.yaml"
    path.write_text(
        """runtime:
  data_root: user_local
identity:
  avatar_model_id: managed-nemesia
providers:
  memory:
    db_path: data/memory.db
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    launch:
      enabled: true
      executable_path: stage/airi.exe
      expected_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      expected_app_asar_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      expected_godot_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      model_path: assets/nemesia.vrm
      expected_model_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      model_id: managed-nemesia
""",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_yaml(path)

    assert config.memory_config is not None
    assert config.memory_config.db_path == str((data_root / "data/memory.db").resolve())
    assert config.avatar_stage_launch_config is not None
    assert config.avatar_stage_launch_config.executable_path == str(
        (config_dir / "stage/airi.exe").resolve()
    )


@pytest.mark.parametrize("mode", ["unknown", "", "program_files"])
def test_invalid_runtime_data_root_fails_closed(tmp_path, mode) -> None:
    path = tmp_path / "invalid-runtime-root.yaml"
    path.write_text(f"runtime:\n  data_root: {mode!r}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime.data_root"):
        RuntimeConfig.from_yaml(path)


def test_relative_runtime_directory_override_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_RUNTIME_DIR", "relative-profile")
    path = tmp_path / "companion.yaml"
    path.write_text("runtime:\n  data_root: user_local\n", encoding="utf-8")

    with pytest.raises(ValueError, match="COMPANION_RUNTIME_DIR must be an absolute path"):
        RuntimeConfig.from_yaml(path)


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
    ("payload", "field"),
    [
        ({"providres": {}}, "providres"),
        ({"providers": {"avater": {}}}, "providers.*avater"),
        ({"providers": {"avatar": {"enabld": True}}}, "providers.avatar.*enabld"),
        (
            {"providers": {"action": {"sandbox_enable": True}}},
            "providers.action.*sandbox_enable",
        ),
        ({"policy": {"quiet_hours": {"starts": "23:00"}}}, "policy.quiet_hours.*starts"),
        ({"dev": {"log_backup_counts": 5}}, "dev.*log_backup_counts"),
    ],
)
def test_unknown_configuration_fields_fail_closed(tmp_path, payload, field) -> None:
    path = tmp_path / "unknown-field.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
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

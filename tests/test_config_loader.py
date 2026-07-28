"""Configuration loading must work from an installed package in any working directory."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from companion.config_loader import DEFAULT_CONFIG_PATH, RuntimeConfig
from companion.providers.implementations.websocket_avatar import WebSocketAvatarConfig


def test_packaged_default_config_is_cwd_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = RuntimeConfig.from_yaml()

    assert DEFAULT_CONFIG_PATH.is_file()
    assert config.identity is not None
    assert config.identity.name == "未命名伙伴"
    assert config.llm_config is not None
    assert config.llm_config.api_key_env == "DEEPSEEK_API_KEY"
    assert config.llm_config.model == "deepseek-chat"


def test_missing_explicit_config_falls_back_without_using_cwd(tmp_path) -> None:
    config = RuntimeConfig.from_yaml(tmp_path / "missing.yaml")

    assert config.identity is None
    assert config.llm_config is None


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
    assert config.action_audit_db_path == audit_path.as_posix()


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

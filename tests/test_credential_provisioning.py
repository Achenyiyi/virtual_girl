"""CLI tests for safe local Avatar Bridge credential provisioning."""

from __future__ import annotations

from argparse import Namespace

import pytest

from companion.__main__ import async_main


def _args(config, *, provision: bool = False, rotate: bool = False) -> Namespace:
    return Namespace(
        config=config,
        verify_memory_backup=None,
        provision_avatar_token=provision,
        rotate_avatar_token=rotate,
        validate_config=False,
    )


def _write_avatar_config(path) -> None:
    path.write_text(
        """providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6122/ws
    auth_token_env: TEST_AVATAR_TOKEN
    credential_target: VirtualCompanion/AvatarBridge
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cli_provisions_configured_avatar_target_without_echoing_secret(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "companion.yaml"
    _write_avatar_config(path)
    monkeypatch.delenv("TEST_AVATAR_TOKEN", raising=False)
    calls: list[tuple[str, bool]] = []

    def provision(target: str, *, overwrite: bool) -> None:
        calls.append((target, overwrite))

    monkeypatch.setattr("companion.__main__.provision_avatar_bridge_credential", provision)

    assert await async_main(_args(path, provision=True)) == 0

    output = capsys.readouterr()
    assert calls == [("VirtualCompanion/AvatarBridge", False)]
    assert "provisioned" in output.out
    assert "TEST_AVATAR_TOKEN" not in output.out
    assert output.err == ""


@pytest.mark.asyncio
async def test_cli_requires_explicit_rotation_for_existing_avatar_target(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "companion.yaml"
    _write_avatar_config(path)
    monkeypatch.delenv("TEST_AVATAR_TOKEN", raising=False)

    def existing(_target: str, *, overwrite: bool) -> None:
        assert overwrite is False
        raise FileExistsError

    monkeypatch.setattr("companion.__main__.provision_avatar_bridge_credential", existing)

    assert await async_main(_args(path, provision=True)) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "--rotate-avatar-token" in output.err


@pytest.mark.asyncio
async def test_cli_rotation_is_explicit_and_environment_override_is_rejected(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "companion.yaml"
    _write_avatar_config(path)
    monkeypatch.delenv("TEST_AVATAR_TOKEN", raising=False)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "companion.__main__.provision_avatar_bridge_credential",
        lambda target, *, overwrite: calls.append((target, overwrite)),
    )

    assert await async_main(_args(path, rotate=True)) == 0
    assert calls == [("VirtualCompanion/AvatarBridge", True)]

    monkeypatch.setenv("TEST_AVATAR_TOKEN", "temporary-override")
    with pytest.raises(ValueError, match="environment override"):
        await async_main(_args(path, provision=True))

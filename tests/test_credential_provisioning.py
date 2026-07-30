"""CLI tests for safe local credential provisioning."""

from __future__ import annotations

from argparse import Namespace

import pytest

from companion.__main__ import async_main


def _args(
    config,
    *,
    provision: bool = False,
    rotate: bool = False,
    set_llm: bool = False,
    rotate_llm: bool = False,
    set_tts: bool = False,
    rotate_tts: bool = False,
) -> Namespace:
    return Namespace(
        config=config,
        verify_memory_backup=None,
        set_llm_credential=set_llm,
        rotate_llm_credential=rotate_llm,
        set_tts_credential=set_tts,
        rotate_tts_credential=rotate_tts,
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


def _write_provider_config(
    path,
    *,
    llm_target: str = "VirtualCompanion/DeepSeek",
    tts_target: str = "VirtualCompanion/FishAudio",
) -> None:
    path.write_text(
        f"""providers:
  llm:
    type: cloud
    cloud:
      provider: openai_compatible
      model: deepseek-chat
      api_key_env: TEST_LLM_CREDENTIAL
      credential_target: "{llm_target}"
      base_url: https://api.deepseek.com/v1/chat/completions
  tts:
    type: cloud
    providers:
      cloud:
        enabled: true
        provider: fish_audio
        model: s2.1-pro-free
        reference_id: ""
        api_key_env: TEST_TTS_CREDENTIAL
        credential_target: "{tts_target}"
        base_url: https://api.fish.audio
        latency: normal
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
async def test_cli_sets_configured_llm_credential_without_echoing_value(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path)
    monkeypatch.delenv("TEST_LLM_CREDENTIAL", raising=False)
    entries = iter(["unit-test-value", "unit-test-value"])
    calls: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: next(entries),
    )
    monkeypatch.setattr(
        "companion.__main__.write_windows_credential",
        lambda target, value, *, overwrite: calls.append((target, value, overwrite)),
    )

    assert await async_main(_args(path, set_llm=True)) == 0

    output = capsys.readouterr()
    assert calls == [("VirtualCompanion/DeepSeek", "unit-test-value", False)]
    assert "VirtualCompanion/DeepSeek" in output.out
    assert "unit-test-value" not in output.out
    assert "unit-test-value" not in output.err


@pytest.mark.asyncio
async def test_cli_rotates_configured_tts_credential(tmp_path, monkeypatch) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path)
    monkeypatch.delenv("TEST_TTS_CREDENTIAL", raising=False)
    entries = iter(["unit-test-value", "unit-test-value"])
    calls: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: next(entries),
    )
    monkeypatch.setattr(
        "companion.__main__.write_windows_credential",
        lambda target, value, *, overwrite: calls.append((target, value, overwrite)),
    )

    assert await async_main(_args(path, rotate_tts=True)) == 0
    assert calls == [("VirtualCompanion/FishAudio", "unit-test-value", True)]


@pytest.mark.asyncio
async def test_cli_requires_explicit_rotation_for_existing_llm_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path)
    monkeypatch.delenv("TEST_LLM_CREDENTIAL", raising=False)
    entries = iter(["unit-test-value", "unit-test-value"])

    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: next(entries),
    )

    def existing(_target: str, _value: str, *, overwrite: bool) -> None:
        assert overwrite is False
        raise FileExistsError

    monkeypatch.setattr("companion.__main__.write_windows_credential", existing)

    assert await async_main(_args(path, set_llm=True)) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "--rotate-llm-credential" in output.err
    assert "unit-test-value" not in output.err


@pytest.mark.asyncio
async def test_cli_rejects_provider_environment_override_before_prompt(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path)
    monkeypatch.setenv("TEST_TTS_CREDENTIAL", "temporary-override")
    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: pytest.fail("provider override should block prompting"),
    )

    with pytest.raises(ValueError, match="environment override"):
        await async_main(_args(path, set_tts=True))


@pytest.mark.asyncio
async def test_cli_requires_provider_credential_target(tmp_path, monkeypatch) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path, llm_target="")
    monkeypatch.delenv("TEST_LLM_CREDENTIAL", raising=False)
    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: pytest.fail("missing target should block prompting"),
    )

    with pytest.raises(ValueError, match="credential_target"):
        await async_main(_args(path, set_llm=True))


@pytest.mark.asyncio
async def test_cli_rejects_mismatched_provider_credential_confirmation(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "companion.yaml"
    _write_provider_config(path)
    monkeypatch.delenv("TEST_LLM_CREDENTIAL", raising=False)
    entries = iter(["first-entry", "second-entry"])
    calls: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        "companion.__main__.getpass.getpass",
        lambda _prompt: next(entries),
    )
    monkeypatch.setattr(
        "companion.__main__.write_windows_credential",
        lambda target, value, *, overwrite: calls.append((target, value, overwrite)),
    )

    with pytest.raises(ValueError, match="entries did not match"):
        await async_main(_args(path, set_llm=True))
    assert calls == []


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

"""Security boundary tests for Windows Credential Manager integration."""

from __future__ import annotations

import pytest

from companion.security.windows_credentials import (
    _decode_credential_blob,
    configured_secret_sources,
    resolve_secret,
)


def test_environment_override_wins_without_reading_windows_store(monkeypatch) -> None:
    monkeypatch.setenv("TEST_SECURE_SECRET", "environment-secret")

    def unexpected_read(_target: str) -> str:
        raise AssertionError("Credential Manager must not be read when the override exists")

    monkeypatch.setattr(
        "companion.security.windows_credentials.read_windows_credential",
        unexpected_read,
    )

    resolved = resolve_secret(
        env_name="TEST_SECURE_SECRET", credential_target="VirtualCompanion/Test"
    )

    assert resolved.value == "environment-secret"
    assert resolved.source == "environment variable TEST_SECURE_SECRET"


def test_windows_credential_is_used_when_environment_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("TEST_SECURE_SECRET", raising=False)
    monkeypatch.setattr(
        "companion.security.windows_credentials.read_windows_credential",
        lambda target: "manager-secret" if target == "VirtualCompanion/Test" else "",
    )

    resolved = resolve_secret(
        env_name="TEST_SECURE_SECRET", credential_target="VirtualCompanion/Test"
    )

    assert resolved.value == "manager-secret"
    assert resolved.source == "Windows Credential Manager target VirtualCompanion/Test"


def test_missing_secret_reports_no_value_without_echoing_content(monkeypatch) -> None:
    monkeypatch.delenv("TEST_SECURE_SECRET", raising=False)
    monkeypatch.setattr(
        "companion.security.windows_credentials.read_windows_credential",
        lambda _target: "",
    )

    resolved = resolve_secret(
        env_name="TEST_SECURE_SECRET", credential_target="VirtualCompanion/Test"
    )

    assert resolved.value == ""
    assert resolved.source == ""
    assert configured_secret_sources(
        env_name="TEST_SECURE_SECRET", credential_target="VirtualCompanion/Test"
    ) == (
        "environment variable TEST_SECURE_SECRET or Windows Credential Manager target "
        "VirtualCompanion/Test"
    )


@pytest.mark.parametrize(
    ("env_name", "target"),
    [
        ("BAD-NAME", "VirtualCompanion/Test"),
        ("TEST_SECRET", " leading-space"),
        ("TEST_SECRET", "bad\ncontrol"),
        ("TEST_SECRET", "x" * 257),
    ],
)
def test_unsafe_credential_references_are_rejected(env_name, target) -> None:
    with pytest.raises(ValueError):
        configured_secret_sources(env_name=env_name, credential_target=target)


def test_windows_credential_blob_requires_utf16() -> None:
    assert _decode_credential_blob("rotated-secret".encode("utf-16-le")) == "rotated-secret"
    assert _decode_credential_blob(b"\xff") == ""

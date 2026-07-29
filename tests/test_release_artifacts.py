from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_release_artifacts import normalize, verify_names


def test_rejects_unsafe_archive_path() -> None:
    with pytest.raises(ValueError, match="unsafe archive path"):
        normalize("../credential.txt")


@pytest.mark.parametrize(
    "name",
    [
        "virtual_companion/data/memory.db",
        "virtual_companion/deepseek_key.txt",
        "virtual_companion/tests/test_runtime.py",
        "virtual_companion/release-evidence/v1.0.0/voice-acceptance.json",
    ],
)
def test_rejects_private_release_content(name: str) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        verify_names(Path("release.whl"), [name], wheel=False)


def test_wheel_requires_runtime_assets() -> None:
    names = [
        "companion/__init__.py",
        "companion/resources/default.yaml",
        "virtual_companion-0.1.0.dist-info/METADATA",
        "virtual_companion-0.1.0.dist-info/licenses/LICENSE",
    ]

    verify_names(Path("release.whl"), names, wheel=True)

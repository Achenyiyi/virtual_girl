from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_release_artifacts import (
    normalize,
    verify_names,
    verify_release_artifacts,
)


def test_rejects_unsafe_archive_path() -> None:
    with pytest.raises(ValueError, match="unsafe archive path"):
        normalize("../credential.txt")

    with pytest.raises(ValueError, match="unsafe archive path"):
        normalize("C:/credential.txt")


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


def test_release_checksums_include_the_staged_windows_installer(tmp_path) -> None:
    wheel = tmp_path / "virtual_companion-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in (
            "companion/__init__.py",
            "companion/resources/default.yaml",
            "virtual_companion-1.2.3.dist-info/METADATA",
            "virtual_companion-1.2.3.dist-info/licenses/LICENSE",
        ):
            archive.writestr(name, "fixture")
    sdist = tmp_path / "virtual_companion-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "virtual_companion-1.2.3/requirements.lock",
            "virtual_companion-1.2.3/requirements-runtime.lock",
        ):
            info = tarfile.TarInfo(name)
            info.size = len(b"fixture")
            archive.addfile(info, io.BytesIO(b"fixture"))
    installer = tmp_path / "VirtualCompanion-1.2.3-windows-x64.exe"
    installer.write_bytes(b"signed installer fixture")

    artifacts = verify_release_artifacts(tmp_path, installer)

    assert {path.name for path in artifacts} == {
        wheel.name,
        sdist.name,
        installer.name,
    }
    checksums = (tmp_path / "SHA256SUMS").read_text(encoding="ascii")
    assert installer.name in checksums


def test_release_verifier_rejects_linked_archive_members(tmp_path) -> None:
    wheel = tmp_path / "virtual_companion-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in (
            "companion/__init__.py",
            "companion/resources/default.yaml",
            "virtual_companion-1.2.3.dist-info/METADATA",
            "virtual_companion-1.2.3.dist-info/licenses/LICENSE",
        ):
            archive.writestr(name, "fixture")
        linked = zipfile.ZipInfo("companion/linked.py")
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(linked, "outside.py")
    sdist = tmp_path / "virtual_companion-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "virtual_companion-1.2.3/requirements.lock",
            "virtual_companion-1.2.3/requirements-runtime.lock",
        ):
            info = tarfile.TarInfo(name)
            info.size = len(b"fixture")
            archive.addfile(info, io.BytesIO(b"fixture"))

    with pytest.raises(ValueError, match="linked wheel member"):
        verify_release_artifacts(tmp_path)

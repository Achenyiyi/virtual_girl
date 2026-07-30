"""Fail release builds that contain private runtime data or incomplete package assets."""

from __future__ import annotations

import argparse
import re
import stat
import tarfile
import zipfile
from hashlib import file_digest
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    "data",
    "tests",
    "release-evidence",
    "__pycache__",
    ".git",
    ".venv",
}
FORBIDDEN_SUFFIXES = {".db", ".key", ".pem", ".pyc", ".pyo"}
REQUIRED_WHEEL_SUFFIXES = {
    "companion/__init__.py",
    "companion/resources/default.yaml",
    "METADATA",
    "licenses/LICENSE",
}
INSTALLER_NAME_PATTERN = re.compile(
    r"VirtualCompanion-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-windows-x64\.exe"
)


def normalize(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in name:
        raise ValueError(f"unsafe archive path: {name}")
    return path


def verify_names(artifact: Path, names: list[str], *, wheel: bool) -> None:
    normalized = [normalize(name) for name in names if name and not name.endswith("/")]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{artifact.name}: duplicate archive path")
    for path in normalized:
        lowered_parts = {part.lower() for part in path.parts}
        lowered_name = path.name.lower()
        if lowered_parts & FORBIDDEN_PARTS:
            raise ValueError(f"{artifact.name}: forbidden directory in {path}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or "key" in lowered_name:
            raise ValueError(f"{artifact.name}: forbidden private file {path}")

    if wheel:
        missing = {
            suffix
            for suffix in REQUIRED_WHEEL_SUFFIXES
            if not any(str(path).endswith(suffix) for path in normalized)
        }
        if missing:
            raise ValueError(f"{artifact.name}: missing required files: {sorted(missing)}")
    else:
        required_locks = {"requirements.lock", "requirements-runtime.lock"}
        present_locks = {
            path.name for path in normalized if path.name in required_locks
        }
        missing_locks = required_locks - present_locks
        if missing_locks:
            raise ValueError(
                f"{artifact.name}: lock files are missing: {sorted(missing_locks)}"
            )


def _verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names: list[str] = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ValueError(f"{path.name}: encrypted wheel member {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"{path.name}: linked wheel member {info.filename}")
            names.append(info.filename)
        verify_names(path, names, wheel=True)


def _verify_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        names: list[str] = []
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"{path.name}: linked or special member {member.name}")
            names.append(member.name)
        verify_names(path, names, wheel=False)


def verify_release_artifacts(dist: Path, installer: Path | None = None) -> list[Path]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}")

    if wheels[0].is_symlink() or sdists[0].is_symlink():
        raise ValueError("release archives must be regular files, not links")

    _verify_wheel(wheels[0])
    _verify_sdist(sdists[0])

    artifacts = [*wheels, *sdists]
    if installer is not None:
        if installer.is_symlink():
            raise ValueError("Windows installer must not be a link")
        installer = installer.resolve()
        if installer.parent != dist.resolve() or not installer.is_file():
            raise ValueError("Windows installer must be a regular file inside dist")
        if INSTALLER_NAME_PATTERN.fullmatch(installer.name) is None:
            raise ValueError("Windows installer filename is invalid")
        artifacts.append(installer)

    checksums = []
    for artifact in sorted(artifacts, key=lambda path: path.name):
        with artifact.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {artifact.name}")
    (dist / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="ascii")

    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path, nargs="?", default=Path("dist"))
    parser.add_argument("--installer", type=Path)
    args = parser.parse_args()
    artifacts = verify_release_artifacts(args.dist, args.installer)

    print(f"verified release artifacts: {', '.join(path.name for path in artifacts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fail release builds that contain private runtime data or incomplete package assets."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from hashlib import sha256
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


def normalize(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return path


def verify_names(artifact: Path, names: list[str], *, wheel: bool) -> None:
    normalized = [normalize(name) for name in names if name and not name.endswith("/")]
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
    elif not any(str(path).endswith("/requirements.lock") for path in normalized):
        raise ValueError(f"{artifact.name}: requirements.lock is missing")


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}")

    with zipfile.ZipFile(wheels[0]) as archive:
        verify_names(wheels[0], archive.namelist(), wheel=True)
    with tarfile.open(sdists[0], mode="r:gz") as archive:
        verify_names(sdists[0], archive.getnames(), wheel=False)

    checksums = []
    for artifact in (*wheels, *sdists):
        digest = sha256(artifact.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {artifact.name}")
    (dist / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="ascii")

    print(f"verified release artifacts: {wheels[0].name}, {sdists[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

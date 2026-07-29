"""Verify that source, tag, and built-package versions describe one release."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def source_versions(root: Path) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = str(project["project"]["version"])
    init_text = (root / "companion" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']$', init_text, re.MULTILINE)
    if not match:
        raise ValueError("companion.__version__ is missing")
    return project_version, match.group(1)


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError(f"{wheel.name}: expected exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    version = metadata.get("Version")
    if not version:
        raise ValueError(f"{wheel.name}: package version is missing")
    return version


def verify(root: Path, *, tag: str | None = None, wheel: Path | None = None) -> str:
    project_version, runtime_version = source_versions(root)
    if not VERSION_PATTERN.fullmatch(project_version):
        raise ValueError(f"project version is not stable SemVer: {project_version}")
    if runtime_version != project_version:
        raise ValueError(
            f"runtime version {runtime_version} does not match project version {project_version}"
        )
    if tag is not None and tag != f"v{project_version}":
        raise ValueError(f"release tag {tag} does not match version v{project_version}")
    if wheel is not None:
        packaged_version = wheel_version(wheel)
        if packaged_version != project_version:
            raise ValueError(
                f"wheel version {packaged_version} does not match project version {project_version}"
            )
    return project_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag")
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    version = verify(args.root.resolve(), tag=args.tag, wheel=args.wheel)
    print(f"verified release version: {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError) as exc:
        print(f"release version verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

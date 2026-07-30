"""Verify every file in an assembled or installed Windows application bundle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from hashlib import file_digest
from pathlib import Path, PurePosixPath
from typing import Any, cast

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
INNO_EXTRA_PATTERN = re.compile(r"unins[0-9]{3}\.(?:dat|exe|msg)", re.IGNORECASE)
FORBIDDEN_SUFFIXES = {".db", ".db-shm", ".db-wal", ".key", ".pem"}
ALLOWED_PUBLIC_CERTIFICATE_BUNDLES = {
    PurePosixPath("runtime/Lib/site-packages/certifi/cacert.pem"),
    PurePosixPath("runtime/Lib/site-packages/grpc/_cython/_credentials/roots.pem"),
}

type JsonObject = dict[str, Any]


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("bundle file path must be a string")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in value:
        raise ValueError(f"unsafe bundle file path: {value}")
    return path


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def verify_bundle(
    root: Path,
    *,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    allow_inno_uninstaller: bool = False,
) -> JsonObject:
    if not root.is_dir() or _is_link_or_junction(root):
        raise ValueError(f"bundle root is not a regular directory: {root}")
    root = root.resolve()
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.is_file() or _is_link_or_junction(manifest_path):
        raise ValueError("bundle-manifest.json is missing")
    value = json.loads(manifest_path.read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise ValueError("bundle manifest root must be an object")
    manifest = cast(JsonObject, value)
    expected_fields = {
        "schema_version",
        "app_version",
        "source_commit",
        "target",
        "python_version",
        "inputs",
        "files",
    }
    if set(manifest) != expected_fields:
        raise ValueError("bundle manifest fields are incomplete or unexpected")
    if (
        manifest["schema_version"] != 1
        or manifest["target"] != "windows-x86_64"
        or manifest["python_version"] != "3.12.10"
    ):
        raise ValueError("bundle manifest schema, target, or Python version is unsupported")
    version = manifest["app_version"]
    commit = manifest["source_commit"]
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        raise ValueError("bundle app_version must be stable SemVer")
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValueError("bundle source_commit is invalid")
    if expected_version is not None and version != expected_version:
        raise ValueError("bundle app_version does not match the expected version")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("bundle source_commit does not match the expected commit")
    inputs = manifest["inputs"]
    if not isinstance(inputs, dict):
        raise ValueError("bundle inputs must be an object")
    expected_inputs = {
        "python_embed_sha256",
        "runtime_lock_sha256",
        "full_lock_sha256",
        "wheel_filename",
        "wheel_sha256",
        "windows_stage_evidence_sha256",
        "managed_avatar_sha256",
    }
    if set(inputs) != expected_inputs:
        raise ValueError("bundle input fields are incomplete or unexpected")
    for field in expected_inputs - {"wheel_filename"}:
        digest = inputs[field]
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError(f"bundle input {field} is not a SHA-256 digest")
    wheel_filename = inputs["wheel_filename"]
    if (
        not isinstance(wheel_filename, str)
        or Path(wheel_filename).name != wheel_filename
        or not wheel_filename.endswith(".whl")
    ):
        raise ValueError("bundle wheel filename is unsafe")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("bundle file inventory is empty")
    expected_files: dict[PurePosixPath, tuple[int, str]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "size_bytes", "sha256"}:
            raise ValueError("bundle file inventory entry is invalid")
        entry = cast(JsonObject, raw_entry)
        relative = _safe_relative(entry["path"])
        if relative in expected_files or relative.name == "bundle-manifest.json":
            raise ValueError(f"duplicate or self-referential bundle file: {relative}")
        size = entry["size_bytes"]
        digest = entry["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"bundle file size is invalid: {relative}")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError(f"bundle file digest is invalid: {relative}")
        if (
            relative.suffix.lower() in FORBIDDEN_SUFFIXES
            and relative not in ALLOWED_PUBLIC_CERTIFICATE_BUNDLES
        ):
            raise ValueError(f"bundle contains private runtime data: {relative}")
        expected_files[relative] = (size, digest)
    actual_files: dict[PurePosixPath, Path] = {}
    for path in root.rglob("*"):
        if path.is_dir():
            if _is_link_or_junction(path):
                raise ValueError(f"bundle contains a link or junction: {path}")
            continue
        if _is_link_or_junction(path) or not path.is_file():
            raise ValueError(f"bundle contains an unsupported file: {path}")
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if relative.name == "bundle-manifest.json":
            continue
        if (
            allow_inno_uninstaller
            and len(relative.parts) == 1
            and INNO_EXTRA_PATTERN.fullmatch(relative.name) is not None
        ):
            continue
        actual_files[relative] = path
    missing = set(expected_files) - set(actual_files)
    unexpected = set(actual_files) - set(expected_files)
    if missing or unexpected:
        raise ValueError(
            f"bundle file set differs: missing={sorted(map(str, missing))}, "
            f"unexpected={sorted(map(str, unexpected))}"
        )
    for relative, (expected_size, expected_digest) in expected_files.items():
        path = actual_files[relative]
        if path.stat().st_size != expected_size or _sha256(path) != expected_digest:
            raise ValueError(f"bundle file integrity mismatch: {relative}")
    linked_inputs = {
        "runtime_lock_sha256": PurePosixPath("provenance/requirements-runtime.lock"),
        "windows_stage_evidence_sha256": PurePosixPath(
            "provenance/windows-stage.json"
        ),
        "managed_avatar_sha256": PurePosixPath("model/managed-avatar.vrm"),
    }
    for field, relative in linked_inputs.items():
        if relative not in actual_files or _sha256(actual_files[relative]) != inputs[field]:
            raise ValueError(f"bundle input {field} does not match {relative}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-commit")
    parser.add_argument("--allow-inno-uninstaller", action="store_true")
    args = parser.parse_args()
    manifest = verify_bundle(
        args.root,
        expected_version=args.expected_version,
        expected_commit=args.expected_commit,
        allow_inno_uninstaller=args.allow_inno_uninstaller,
    )
    print(
        f"verified Windows bundle {manifest['app_version']} "
        f"from {manifest['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Windows bundle verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

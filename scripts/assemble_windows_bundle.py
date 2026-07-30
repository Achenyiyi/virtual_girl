"""Assemble the verified Windows application bundle consumed by Inno Setup."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.parser import Parser
from hashlib import file_digest
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
LOCK_REQUIREMENT_PATTERN = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", re.MULTILINE
)
FORBIDDEN_RUNTIME_PACKAGES = {
    "build",
    "detect-secrets",
    "hypothesis",
    "mypy",
    "pip-audit",
    "pip-tools",
    "pre-commit",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-timeout",
    "ruff",
}
REQUIRED_RUNTIME_PACKAGES = {
    "faster-whisper",
    "numpy",
    "pydantic",
    "pyyaml",
    "sounddevice",
}
RUNTIME_SDIST_ALLOWLIST = {"jieba"}

type JsonObject = dict[str, Any]
type DependencyInstaller = Callable[[Path, Path, Path, Path], None]


def _load_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: root must be an object")
    return cast(JsonObject, value)


def _require_exact_fields(
    value: Mapping[str, object], expected: set[str], context: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _require_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _ensure_regular_file(path: Path, context: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{context} must be a regular file: {path}")
    return path.resolve()


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _validate_regular_tree(root: Path, context: str) -> Path:
    if not root.is_dir() or _is_link_or_junction(root):
        raise ValueError(f"{context} must be a regular directory: {root}")
    resolved = root.resolve()
    for entry in resolved.rglob("*"):
        if _is_link_or_junction(entry):
            raise ValueError(f"{context} contains a link or junction: {entry}")
        mode = entry.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"{context} contains an unsupported entry: {entry}")
    return resolved


def _normalized_packages(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    packages: dict[str, str] = {}
    for name, version in LOCK_REQUIREMENT_PATTERN.findall(text):
        normalized = name.lower().replace("_", "-").replace(".", "-")
        if normalized in packages:
            raise ValueError(f"{path.name}: duplicate locked package {normalized}")
        packages[normalized] = version
    if not packages:
        raise ValueError(f"{path.name}: no pinned requirements found")
    return packages


def validate_runtime_lock(runtime_lock: Path, full_lock: Path) -> dict[str, str]:
    """Require a runtime-only hash lock whose versions are constrained by the full lock."""
    runtime_lock = _ensure_regular_file(runtime_lock, "runtime lock")
    full_lock = _ensure_regular_file(full_lock, "full lock")
    runtime_text = runtime_lock.read_text(encoding="utf-8")
    runtime = _normalized_packages(runtime_lock)
    full = _normalized_packages(full_lock)
    forbidden = set(runtime) & FORBIDDEN_RUNTIME_PACKAGES
    if forbidden:
        raise ValueError(f"runtime lock includes non-runtime tools: {sorted(forbidden)}")
    missing = REQUIRED_RUNTIME_PACKAGES - set(runtime)
    if missing:
        raise ValueError(f"runtime lock is missing required packages: {sorted(missing)}")
    drift = {
        name: (version, full.get(name))
        for name, version in runtime.items()
        if full.get(name) != version
    }
    if drift:
        raise ValueError(f"runtime lock is not constrained by requirements.lock: {drift}")
    for match in LOCK_REQUIREMENT_PATTERN.finditer(runtime_text):
        next_match = LOCK_REQUIREMENT_PATTERN.search(runtime_text, match.end())
        block = runtime_text[match.start() : next_match.start() if next_match else None]
        if "--hash=sha256:" not in block:
            raise ValueError(f"runtime lock entry lacks hashes: {match.group(1)}")
    return runtime


def validate_toolchain_manifest(path: Path) -> JsonObject:
    manifest = _load_object(_ensure_regular_file(path, "toolchain manifest"))
    _require_exact_fields(
        manifest, {"schema_version", "target", "python", "inno_setup", "signing"}, "toolchain"
    )
    if manifest["schema_version"] != 1:
        raise ValueError("toolchain schema_version is unsupported")
    target = cast(JsonObject, manifest["target"])
    python = cast(JsonObject, manifest["python"])
    inno = cast(JsonObject, manifest["inno_setup"])
    signing = cast(JsonObject, manifest["signing"])
    _require_exact_fields(target, {"os", "architecture", "minimum_version"}, "target")
    _require_exact_fields(
        python,
        {
            "version",
            "implementation",
            "architecture",
            "embed_url",
            "embed_sha256",
            "pth_file",
        },
        "python",
    )
    _require_exact_fields(
        inno,
        {
            "version",
            "architecture",
            "installer_url",
            "installer_sha256",
            "publisher",
            "compiler_relative_path",
            "license_url",
            "license_allows_commercial_use",
            "commercial_license_request_url",
        },
        "inno_setup",
    )
    _require_exact_fields(
        signing,
        {
            "file_digest_algorithm",
            "timestamp_digest_algorithm",
            "timestamp_url",
            "required_artifacts",
        },
        "signing",
    )
    if target != {
        "os": "windows",
        "architecture": "x86_64",
        "minimum_version": "10.0.19041",
    }:
        raise ValueError("toolchain target is not the approved Windows x64 target")
    if python["version"] != "3.12.10" or python["pth_file"] != "python312._pth":
        raise ValueError("toolchain does not pin the approved CPython runtime")
    _require_sha256(python["embed_sha256"], "python.embed_sha256")
    _require_sha256(inno["installer_sha256"], "inno_setup.installer_sha256")
    if inno["version"] != "7.0.2" or inno["publisher"] != "Pyrsys B.V.":
        raise ValueError("toolchain does not pin the approved Inno Setup release")
    if inno["license_allows_commercial_use"] is not True:
        raise ValueError("toolchain Inno Setup license does not allow commercial use")
    if signing["file_digest_algorithm"] != "SHA256":
        raise ValueError("toolchain signing digest must be SHA256")
    if signing["timestamp_digest_algorithm"] != "SHA256":
        raise ValueError("toolchain timestamp digest must be SHA256")
    if signing["required_artifacts"] != [
        "airi/airi.exe",
        "airi/resources/godot-stage/godot-stage.exe",
        "installer",
        "uninstaller",
    ]:
        raise ValueError("toolchain signed artifact set is not approved")
    return manifest


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            member = PurePosixPath(info.filename.replace("\\", "/"))
            if member.is_absolute() or ".." in member.parts or not member.parts:
                raise ValueError(f"unsafe embedded Python archive path: {info.filename}")
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise ValueError(f"embedded Python archive contains a link: {info.filename}")
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def prepare_embedded_python(
    archive: Path, destination: Path, toolchain: Mapping[str, object]
) -> None:
    archive = _ensure_regular_file(archive, "CPython embeddable archive")
    python = cast(JsonObject, toolchain["python"])
    expected = _require_sha256(python["embed_sha256"], "python.embed_sha256")
    if _file_sha256(archive) != expected:
        raise ValueError("CPython embeddable archive SHA-256 mismatch")
    destination.mkdir(parents=True, exist_ok=False)
    _safe_extract_zip(archive, destination)
    for name in ("python.exe", "pythonw.exe", "python312.zip"):
        _ensure_regular_file(destination / name, f"embedded Python {name}")
    pth = _ensure_regular_file(destination / str(python["pth_file"]), "Python ._pth")
    pth.write_text(
        "python312.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii", newline="\n"
    )


def _validate_project_wheel(path: Path, version: str) -> None:
    path = _ensure_regular_file(path, "project wheel")
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("project wheel must contain exactly one METADATA file")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    if metadata.get("Name", "").lower().replace("_", "-") != "virtual-companion":
        raise ValueError("project wheel name is not virtual-companion")
    if metadata.get("Version") != version:
        raise ValueError("project wheel version does not match the installer version")


def _default_dependency_installer(
    pip_python: Path, runtime_lock: Path, wheel: Path, site_packages: Path
) -> None:
    site_packages.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment.update({"PIP_NO_INPUT": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    common = [
        str(pip_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-compile",
        "--no-deps",
        "--no-build-isolation",
        "--only-binary=:all:",
        f"--no-binary={','.join(sorted(RUNTIME_SDIST_ALLOWLIST))}",
        "--target",
        str(site_packages),
    ]
    subprocess.run(
        [*common, "--require-hashes", "-r", str(runtime_lock)],
        check=True,
        env=environment,
    )
    subprocess.run([*common, str(wheel)], check=True, env=environment)


def _validate_model(model: Path, manifest: Mapping[str, object]) -> None:
    model = _ensure_regular_file(model, "managed avatar")
    if manifest.get("schema_version") != 2:
        raise ValueError("managed avatar manifest schema is unsupported")
    expected_size = manifest.get("size_bytes")
    expected_digest = _require_sha256(manifest.get("sha256"), "managed avatar sha256")
    if model.stat().st_size != expected_size or _file_sha256(model) != expected_digest:
        raise ValueError("managed avatar does not match its approved size and digest")
    with model.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20 or header[:4] != b"glTF":
            raise ValueError("managed avatar is not GLB 2.0")
        version = int.from_bytes(header[4:8], "little")
        length = int.from_bytes(header[8:12], "little")
        json_length = int.from_bytes(header[12:16], "little")
        if version != 2 or length != model.stat().st_size or header[16:20] != b"JSON":
            raise ValueError("managed avatar GLB header is invalid")
        document = json.loads(
            stream.read(json_length).decode("utf-8").rstrip("\x00 \t\r\n")
        )
    license_value = manifest.get("license")
    if not isinstance(license_value, dict):
        raise ValueError("managed avatar license is missing")
    embedded = document.get("extensions", {}).get("VRM", {}).get("meta", {})
    comparisons = {
        "title": "title",
        "author": "author",
        "allowedUserName": "allowed_user_name",
        "commercialUssageName": "commercial_usage_name",
        "licenseName": "license_name",
        "otherPermissionUrl": "license_url",
        "otherLicenseUrl": "license_url",
    }
    if any(embedded.get(left) != license_value.get(right) for left, right in comparisons.items()):
        raise ValueError("managed avatar embedded license does not match its manifest")
    permissions = license_value.get("permissions")
    required_permissions = {
        "corporate_commercial_use": True,
        "personal_commercial_use": True,
        "redistribution": True,
        "modification": True,
        "credit_required": False,
    }
    if permissions != required_permissions:
        raise ValueError("managed avatar license does not authorize installer distribution")


def _validate_stage_evidence(
    evidence_path: Path,
    stage: Path,
    model: Path,
    model_manifest: Mapping[str, object],
    app_version: str,
) -> JsonObject:
    evidence = _load_object(_ensure_regular_file(evidence_path, "Windows stage evidence"))
    _require_exact_fields(
        evidence,
        {
            "schema_version",
            "app_version",
            "generated_at",
            "passed",
            "artifact_sha256",
            "authenticode",
            "model_license",
        },
        "Windows stage evidence",
    )
    if evidence["schema_version"] != 1 or evidence["app_version"] != app_version:
        raise ValueError("Windows stage evidence version is invalid")
    if evidence["passed"] is not True:
        raise ValueError("Windows stage evidence did not pass")
    try:
        generated_at = datetime.fromisoformat(str(evidence["generated_at"]).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Windows stage evidence timestamp is invalid") from None
    if generated_at.tzinfo is None:
        raise ValueError("Windows stage evidence timestamp lacks a timezone")
    now = datetime.now(UTC)
    generated_at = generated_at.astimezone(UTC)
    if generated_at > now or generated_at < now - timedelta(days=30):
        raise ValueError("Windows stage evidence is stale or future-dated")
    artifacts = cast(JsonObject, evidence["artifact_sha256"])
    _require_exact_fields(
        artifacts,
        {"airi_exe", "app_asar", "godot_stage_exe", "managed_avatar"},
        "Windows stage artifact_sha256",
    )
    expected_files = {
        "airi_exe": stage / "airi.exe",
        "app_asar": stage / "resources" / "app.asar",
        "godot_stage_exe": stage / "resources" / "godot-stage" / "godot-stage.exe",
        "managed_avatar": model,
    }
    for name, path in expected_files.items():
        expected = _require_sha256(artifacts.get(name), f"Windows stage {name}")
        if _file_sha256(_ensure_regular_file(path, name)) != expected:
            raise ValueError(f"Windows stage evidence digest mismatch for {name}")
    signatures = cast(JsonObject, evidence["authenticode"])
    _require_exact_fields(signatures, {"airi_exe", "godot_stage_exe"}, "authenticode")
    for name in signatures:
        signature = cast(JsonObject, signatures[name])
        _require_exact_fields(
            signature,
            {"status", "signer_certificate_sha256", "timestamp_certificate_sha256"},
            f"authenticode.{name}",
        )
        if signature["status"] != "Valid":
            raise ValueError(f"authenticode.{name} is not valid")
        _require_sha256(signature["signer_certificate_sha256"], f"{name} signer")
        _require_sha256(signature["timestamp_certificate_sha256"], f"{name} timestamp")
    license_value = cast(JsonObject, model_manifest["license"])
    permissions = cast(JsonObject, license_value["permissions"])
    approved_license = {
        "model_id": model_manifest["model_id"],
        "title": license_value["title"],
        "author": license_value["author"],
        "source": license_value["source"],
        "license_url": license_value["license_url"],
        **permissions,
    }
    if evidence["model_license"] != approved_license:
        raise ValueError("Windows stage evidence contains an unapproved model license")
    return evidence


def _assert_no_secret_values(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "api_key_file", "auth_token"}:
                field = ".".join((*path, str(key)))
                raise ValueError(
                    f"production configuration contains forbidden field {field}"
                )
            _assert_no_secret_values(nested, (*path, str(key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_secret_values(nested, (*path, str(index)))


def _write_production_config(
    default_config: Path,
    destination: Path,
    stage_evidence: Mapping[str, object],
    model_manifest: Mapping[str, object],
) -> None:
    loaded = yaml.safe_load(default_config.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("default production configuration must be a mapping")
    config = cast(JsonObject, loaded)
    identity = cast(JsonObject, config["identity"])
    providers = cast(JsonObject, config["providers"])
    avatar = cast(JsonObject, providers["avatar"])
    launch = cast(JsonObject, avatar["launch"])
    artifacts = cast(JsonObject, stage_evidence["artifact_sha256"])
    config["runtime"] = {"data_root": "user_local"}
    identity["avatar_model_id"] = model_manifest["model_id"]
    avatar["enabled"] = True
    launch.update(
        {
            "enabled": True,
            "executable_path": "../airi/airi.exe",
            "expected_sha256": artifacts["airi_exe"],
            "expected_app_asar_sha256": artifacts["app_asar"],
            "expected_godot_sha256": artifacts["godot_stage_exe"],
            "model_path": "../model/managed-avatar.vrm",
            "expected_model_sha256": artifacts["managed_avatar"],
            "model_id": model_manifest["model_id"],
            "model_name": model_manifest["display_name"],
        }
    )
    action = cast(JsonObject, providers["action"])
    action["enabled"] = False
    _assert_no_secret_values(config)
    destination.parent.mkdir(parents=True, exist_ok=False)
    destination.write_text(
        yaml.safe_dump(
            config,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1000,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_launchers(root: Path) -> None:
    launch = (
        "@echo off\n"
        "setlocal\n"
        '"%~dp0runtime\\python.exe" -I -s -B -m companion '
        '--config "%~dp0config\\production.yaml" --voice-input\n'
        'set "companion_exit=%ERRORLEVEL%"\n'
        'if not "%companion_exit%"=="0" pause\n'
        "exit /b %companion_exit%\n"
    )
    diagnostics = (
        "@echo off\n"
        "setlocal\n"
        '"%~dp0runtime\\python.exe" -I -s -B -m companion '
        '--config "%~dp0config\\production.yaml" --doctor --doctor-online\n'
        'set "companion_exit=%ERRORLEVEL%"\n'
        "pause\n"
        "exit /b %companion_exit%\n"
    )
    (root / "launch-companion.cmd").write_text(launch, encoding="ascii", newline="\r\n")
    (root / "diagnostics.cmd").write_text(diagnostics, encoding="ascii", newline="\r\n")


def _bundle_files(root: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path.is_dir():
            continue
        if _is_link_or_junction(path):
            raise ValueError(f"bundle contains a link or junction: {path}")
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    return files


def assemble_bundle(
    *,
    stage: Path,
    model: Path,
    wheel: Path,
    python_embed: Path,
    runtime_lock: Path,
    full_lock: Path,
    stage_evidence_path: Path,
    model_manifest_path: Path,
    toolchain_path: Path,
    default_config: Path,
    project_license: Path,
    third_party_assets: Path,
    output: Path,
    app_version: str,
    source_commit: str,
    pip_python: Path = Path(sys.executable),
    dependency_installer: DependencyInstaller = _default_dependency_installer,
) -> Path:
    """Build a new bundle atomically; verified inputs are never modified."""
    if SEMVER_PATTERN.fullmatch(app_version) is None:
        raise ValueError("app_version must be stable SemVer")
    if COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ValueError("source_commit must be a lowercase 40-character Git commit")
    stage = _validate_regular_tree(stage, "AIRI stage")
    model = _ensure_regular_file(model, "managed avatar")
    wheel = _ensure_regular_file(wheel, "project wheel")
    python_embed = _ensure_regular_file(python_embed, "CPython embeddable archive")
    runtime_lock = _ensure_regular_file(runtime_lock, "runtime lock")
    full_lock = _ensure_regular_file(full_lock, "full lock")
    validate_runtime_lock(runtime_lock, full_lock)
    toolchain = validate_toolchain_manifest(toolchain_path)
    model_manifest = _load_object(
        _ensure_regular_file(model_manifest_path, "managed avatar manifest")
    )
    _validate_model(model, model_manifest)
    stage_evidence = _validate_stage_evidence(
        stage_evidence_path, stage, model, model_manifest, app_version
    )
    _validate_project_wheel(wheel, app_version)
    for path, context in (
        (default_config, "default configuration"),
        (project_license, "project license"),
        (third_party_assets, "third-party asset notice"),
        (pip_python, "pip Python"),
    ):
        _ensure_regular_file(path, context)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"bundle output already exists: {output}")
    partial = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        shutil.copytree(stage, partial / "airi")
        model_directory = partial / "model"
        model_directory.mkdir()
        shutil.copy2(model, model_directory / "managed-avatar.vrm")
        runtime = partial / "runtime"
        prepare_embedded_python(python_embed, runtime, toolchain)
        dependency_installer(
            pip_python.resolve(), runtime_lock, wheel, runtime / "Lib" / "site-packages"
        )
        _write_production_config(
            default_config, partial / "config" / "production.yaml", stage_evidence, model_manifest
        )
        provenance = partial / "provenance"
        provenance.mkdir()
        shutil.copy2(stage_evidence_path, provenance / "windows-stage.json")
        shutil.copy2(runtime_lock, provenance / "requirements-runtime.lock")
        licenses = partial / "licenses"
        licenses.mkdir()
        shutil.copy2(project_license, licenses / "virtual-companion-MIT.txt")
        shutil.copy2(model_manifest_path, licenses / "managed-avatar.json")
        shutil.copy2(third_party_assets, licenses / "THIRD_PARTY_ASSETS.md")
        _write_launchers(partial)
        inputs = {
            "python_embed_sha256": _file_sha256(python_embed),
            "runtime_lock_sha256": _file_sha256(runtime_lock),
            "full_lock_sha256": _file_sha256(full_lock),
            "wheel_filename": wheel.name,
            "wheel_sha256": _file_sha256(wheel),
            "windows_stage_evidence_sha256": _file_sha256(stage_evidence_path),
            "managed_avatar_sha256": _file_sha256(model),
        }
        manifest = {
            "schema_version": 1,
            "app_version": app_version,
            "source_commit": source_commit,
            "target": "windows-x86_64",
            "python_version": cast(JsonObject, toolchain["python"])["version"],
            "inputs": inputs,
            "files": _bundle_files(partial),
        }
        (partial / "bundle-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="ascii",
            newline="\n",
        )
        from scripts.verify_windows_bundle import verify_bundle

        verify_bundle(partial, expected_version=app_version, expected_commit=source_commit)
        os.replace(partial, output)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python-embed", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, default=Path("requirements-runtime.lock"))
    parser.add_argument("--full-lock", type=Path, default=Path("requirements.lock"))
    parser.add_argument("--stage-evidence", type=Path, required=True)
    parser.add_argument(
        "--model-manifest",
        type=Path,
        default=Path("integrations/airi-v0.11.3/managed-avatar.json"),
    )
    parser.add_argument(
        "--toolchain", type=Path, default=Path("packaging/windows/toolchain.json")
    )
    parser.add_argument(
        "--default-config", type=Path, default=Path("companion/resources/default.yaml")
    )
    parser.add_argument("--project-license", type=Path, default=Path("LICENSE"))
    parser.add_argument(
        "--third-party-assets", type=Path, default=Path("docs/third_party_assets.md")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pip-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    output = assemble_bundle(
        stage=args.stage,
        model=args.model,
        wheel=args.wheel,
        python_embed=args.python_embed,
        runtime_lock=args.runtime_lock,
        full_lock=args.full_lock,
        stage_evidence_path=args.stage_evidence,
        model_manifest_path=args.model_manifest,
        toolchain_path=args.toolchain,
        default_config=args.default_config,
        project_license=args.project_license,
        third_party_assets=args.third_party_assets,
        output=args.output,
        app_version=args.app_version,
        source_commit=args.source_commit,
        pip_python=args.pip_python,
    )
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"Windows bundle assembly failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

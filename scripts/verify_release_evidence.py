"""Require privacy-safe target-machine evidence before publishing a release."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from hashlib import file_digest
from pathlib import Path
from typing import Any

REQUIRED_CHECK_CODES = {
    "voice.": {
        "voice.complete_turn",
        "voice.first_audio_latency",
        "voice.incremental_playback",
        "voice.pcm_continuity",
        "voice.completed_history",
        "voice.interrupt_terminal",
        "voice.interrupt_latency",
        "voice.interrupted_history",
    },
    "avatar.": {
        "avatar.bridge_health",
        "avatar.model_available",
        "avatar.model_loaded",
        "avatar.renderer_ready",
        "avatar.state_rendered",
        "avatar.frame_presented",
    },
}

MANAGED_AVATAR_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "airi-v0.11.3"
    / "managed-avatar.json"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
WINDOWS_ARTIFACTS = {
    "airi_exe",
    "app_asar",
    "godot_stage_exe",
    "managed_avatar",
}
WINDOWS_SIGNED_ARTIFACTS = {"airi_exe", "godot_stage_exe"}
WINDOWS_INSTALLER_SMOKE_CHECKS = {
    "silent_install",
    "bundle_integrity",
    "config_validation",
    "runtime_import",
    "cli_help",
    "uninstaller_authenticode",
    "silent_uninstall",
    "install_directory_removed",
}
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: root must be an object")
    return value


def _parse_timestamp(value: object, path: Path, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path.name}: {field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{path.name}: {field} must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{path.name}: {field} must include a timezone")
    return parsed.astimezone(UTC)


def _require_exact_fields(
    value: dict[str, Any], expected: set[str], path: Path, context: str
) -> None:
    unexpected = set(value) - expected
    if unexpected:
        raise ValueError(f"{path.name}: unexpected {context} fields: {sorted(unexpected)}")
    missing = expected - set(value)
    if missing:
        raise ValueError(f"{path.name}: missing {context} fields: {sorted(missing)}")


def _require_recent(value: object, path: Path, field: str) -> None:
    parsed = _parse_timestamp(value, path, field)
    now = datetime.now(UTC)
    if parsed > now or parsed < now - timedelta(days=30):
        raise ValueError(f"{path.name}: {field} is stale or future-dated")


def _require_sha256(value: object, path: Path, field: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{path.name}: {field} must be a lowercase SHA-256 digest")
    return value


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _approved_model_release_values() -> tuple[str, dict[str, object]]:
    manifest = _load_object(MANAGED_AVATAR_MANIFEST)
    if manifest.get("schema_version") != 2:
        raise ValueError("managed-avatar.json: unsupported schema_version")
    model_sha256 = _require_sha256(
        manifest.get("sha256"), MANAGED_AVATAR_MANIFEST, "sha256"
    )
    license_value = manifest.get("license")
    if not isinstance(license_value, dict):
        raise ValueError("managed-avatar.json: license must be an object")
    permissions = license_value.get("permissions")
    if not isinstance(permissions, dict):
        raise ValueError("managed-avatar.json: license permissions must be an object")
    expected = {
        "model_id": manifest.get("model_id"),
        "title": license_value.get("title"),
        "author": license_value.get("author"),
        "source": license_value.get("source"),
        "license_url": license_value.get("license_url"),
        "corporate_commercial_use": permissions.get("corporate_commercial_use"),
        "personal_commercial_use": permissions.get("personal_commercial_use"),
        "redistribution": permissions.get("redistribution"),
        "modification": permissions.get("modification"),
        "credit_required": permissions.get("credit_required"),
    }
    for field in ("model_id", "title", "author", "source", "license_url"):
        if not isinstance(expected[field], str) or not expected[field]:
            raise ValueError(f"managed-avatar.json: license release field {field} is invalid")
    required_permissions = {
        "corporate_commercial_use": True,
        "personal_commercial_use": True,
        "redistribution": True,
        "modification": True,
        "credit_required": False,
    }
    for field, required in required_permissions.items():
        if expected[field] is not required:
            raise ValueError(f"managed-avatar.json: release permission {field} is invalid")
    return model_sha256, expected


def _verify_windows_stage(path: Path, expected_version: str) -> dict[str, Any]:
    report = _load_object(path)
    _require_exact_fields(
        report,
        {
            "schema_version",
            "app_version",
            "generated_at",
            "passed",
            "artifact_sha256",
            "authenticode",
            "model_license",
        },
        path,
        "report",
    )
    if report["schema_version"] != 1:
        raise ValueError(f"{path.name}: unsupported schema_version")
    if report["app_version"] != expected_version:
        raise ValueError(f"{path.name}: app_version does not match release")
    if report["passed"] is not True:
        raise ValueError(f"{path.name}: Windows stage verification did not pass")
    _require_recent(report["generated_at"], path, "generated_at")

    artifacts = report["artifact_sha256"]
    if not isinstance(artifacts, dict):
        raise ValueError(f"{path.name}: artifact_sha256 must be an object")
    _require_exact_fields(artifacts, WINDOWS_ARTIFACTS, path, "artifact_sha256")
    for artifact in WINDOWS_ARTIFACTS:
        _require_sha256(artifacts[artifact], path, f"artifact_sha256.{artifact}")

    signatures = report["authenticode"]
    if not isinstance(signatures, dict):
        raise ValueError(f"{path.name}: authenticode must be an object")
    _require_exact_fields(signatures, WINDOWS_SIGNED_ARTIFACTS, path, "authenticode")
    signature_fields = {
        "status",
        "signer_certificate_sha256",
        "timestamp_certificate_sha256",
    }
    for artifact in WINDOWS_SIGNED_ARTIFACTS:
        signature = signatures[artifact]
        if not isinstance(signature, dict):
            raise ValueError(f"{path.name}: authenticode.{artifact} must be an object")
        _require_exact_fields(signature, signature_fields, path, f"authenticode.{artifact}")
        if signature["status"] != "Valid":
            raise ValueError(f"{path.name}: authenticode.{artifact}.status must be Valid")
        for field in ("signer_certificate_sha256", "timestamp_certificate_sha256"):
            _require_sha256(signature[field], path, f"authenticode.{artifact}.{field}")

    approved_model_sha256, approved_license = _approved_model_release_values()
    if artifacts["managed_avatar"] != approved_model_sha256:
        raise ValueError(f"{path.name}: managed avatar digest is not approved")
    model_license = report["model_license"]
    if not isinstance(model_license, dict):
        raise ValueError(f"{path.name}: model_license must be an object")
    _require_exact_fields(model_license, set(approved_license), path, "model_license")
    for field, expected in approved_license.items():
        actual = model_license[field]
        matches = actual is expected if isinstance(expected, bool) else actual == expected
        if not matches:
            raise ValueError(f"{path.name}: model_license.{field} is not approved")
    return report


def _verify_windows_installer(path: Path, expected_version: str) -> dict[str, Any]:
    report = _load_object(path)
    _require_exact_fields(
        report,
        {
            "schema_version",
            "app_version",
            "source_commit",
            "generated_at",
            "passed",
            "installer",
            "authenticode",
            "bundle_manifest_sha256",
            "windows_stage_evidence_sha256",
            "smoke",
        },
        path,
        "report",
    )
    if report["schema_version"] != 1:
        raise ValueError(f"{path.name}: unsupported schema_version")
    if report["app_version"] != expected_version:
        raise ValueError(f"{path.name}: app_version does not match release")
    if report["passed"] is not True:
        raise ValueError(f"{path.name}: installer verification did not pass")
    _require_recent(report["generated_at"], path, "generated_at")
    source_commit = report["source_commit"]
    if not isinstance(source_commit, str) or COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ValueError(f"{path.name}: source_commit must be a lowercase Git commit")

    installer = report["installer"]
    if not isinstance(installer, dict):
        raise ValueError(f"{path.name}: installer must be an object")
    _require_exact_fields(
        installer, {"filename", "size_bytes", "sha256"}, path, "installer"
    )
    expected_filename = f"VirtualCompanion-{expected_version}-windows-x64.exe"
    if installer["filename"] != expected_filename:
        raise ValueError(f"{path.name}: installer.filename is not approved")
    size_bytes = installer["size_bytes"]
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise ValueError(f"{path.name}: installer.size_bytes must be positive")
    _require_sha256(installer["sha256"], path, "installer.sha256")

    signature = report["authenticode"]
    if not isinstance(signature, dict):
        raise ValueError(f"{path.name}: authenticode must be an object")
    signature_fields = {
        "status",
        "signer_certificate_sha256",
        "timestamp_certificate_sha256",
    }
    _require_exact_fields(signature, signature_fields, path, "authenticode")
    if signature["status"] != "Valid":
        raise ValueError(f"{path.name}: authenticode.status must be Valid")
    for field in ("signer_certificate_sha256", "timestamp_certificate_sha256"):
        _require_sha256(signature[field], path, f"authenticode.{field}")
    _require_sha256(
        report["bundle_manifest_sha256"], path, "bundle_manifest_sha256"
    )
    _require_sha256(
        report["windows_stage_evidence_sha256"],
        path,
        "windows_stage_evidence_sha256",
    )

    smoke = report["smoke"]
    if not isinstance(smoke, dict):
        raise ValueError(f"{path.name}: smoke must be an object")
    _require_exact_fields(smoke, WINDOWS_INSTALLER_SMOKE_CHECKS, path, "smoke")
    for check in WINDOWS_INSTALLER_SMOKE_CHECKS:
        if smoke[check] is not True:
            raise ValueError(f"{path.name}: smoke.{check} must be true")
    return report


def _verify_acceptance(path: Path, expected_prefix: str, expected_version: str) -> None:
    report = _load_object(path)
    allowed_report_fields = {
        "schema_version",
        "app_version",
        "generated_at",
        "passed",
        "exit_code",
        "checks",
    }
    unexpected_report_fields = set(report) - allowed_report_fields
    if unexpected_report_fields:
        raise ValueError(f"{path.name}: unexpected fields: {sorted(unexpected_report_fields)}")
    if report.get("passed") is not True or report.get("exit_code") != 0:
        raise ValueError(f"{path.name}: acceptance did not pass")
    if report.get("schema_version") != 1:
        raise ValueError(f"{path.name}: unsupported schema_version")
    if report.get("app_version") != expected_version:
        raise ValueError(f"{path.name}: app_version does not match release")
    _require_recent(report.get("generated_at"), path, "generated_at")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"{path.name}: checks are missing")
    check_codes: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError(f"{path.name}: check must be an object")
        allowed_check_fields = {"code", "passed", "message"}
        if expected_prefix == "voice.":
            allowed_check_fields |= {"actual_ms", "target_ms"}
        unexpected_check_fields = set(check) - allowed_check_fields
        if unexpected_check_fields:
            raise ValueError(
                f"{path.name}: unexpected check fields: {sorted(unexpected_check_fields)}"
            )
        code = check.get("code")
        if not isinstance(code, str) or not code.startswith(expected_prefix):
            raise ValueError(f"{path.name}: unexpected check code")
        check_codes.append(code)
        if check.get("passed") is not True:
            raise ValueError(f"{path.name}: check {code} did not pass")
    if len(check_codes) != len(set(check_codes)):
        raise ValueError(f"{path.name}: duplicate check codes")
    if set(check_codes) != REQUIRED_CHECK_CODES[expected_prefix]:
        raise ValueError(f"{path.name}: required acceptance checks are missing or unexpected")


def _verify_signoff(path: Path, required_true: set[str]) -> None:
    signoff = _load_object(path)
    allowed = required_true | {"reviewer", "observed_at"}
    unexpected = set(signoff) - allowed
    if unexpected:
        raise ValueError(f"{path.name}: unexpected fields: {sorted(unexpected)}")
    for field in required_true:
        if signoff.get(field) is not True:
            raise ValueError(f"{path.name}: {field} must be true")
    reviewer = signoff.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError(f"{path.name}: reviewer is required")
    _require_recent(signoff.get("observed_at"), path, "observed_at")


def verify(evidence_root: Path, tag: str) -> Path:
    if not tag.startswith("v"):
        raise ValueError("release tag must start with v")
    expected_version = tag[1:]
    evidence = evidence_root / tag
    required = {
        "voice-acceptance.json",
        "avatar-acceptance.json",
        "windows-stage.json",
        "windows-installer.json",
        "visual-signoff.json",
        "security-signoff.json",
    }
    if not evidence.is_dir():
        raise ValueError(f"release evidence directory is missing: {evidence}")
    entries = {path.name for path in evidence.iterdir()}
    unexpected = entries - required
    if unexpected:
        raise ValueError(f"release evidence has unexpected entries: {sorted(unexpected)}")
    names = {path.name for path in evidence.iterdir() if path.is_file()}
    missing = required - names
    if missing:
        raise ValueError(f"release evidence is missing: {sorted(missing)}")
    _verify_acceptance(evidence / "voice-acceptance.json", "voice.", expected_version)
    _verify_acceptance(evidence / "avatar-acceptance.json", "avatar.", expected_version)
    windows_stage = _verify_windows_stage(
        evidence / "windows-stage.json", expected_version
    )
    windows_installer = _verify_windows_installer(
        evidence / "windows-installer.json", expected_version
    )
    expected_stage_evidence_sha256 = _file_sha256(evidence / "windows-stage.json")
    if (
        windows_installer["windows_stage_evidence_sha256"]
        != expected_stage_evidence_sha256
    ):
        raise ValueError(
            "windows-installer.json: windows_stage_evidence_sha256 does not match "
            "windows-stage.json"
        )
    signer_digests = {
        windows_stage["authenticode"][artifact]["signer_certificate_sha256"]
        for artifact in WINDOWS_SIGNED_ARTIFACTS
    }
    signer_digests.add(
        windows_installer["authenticode"]["signer_certificate_sha256"]
    )
    if len(signer_digests) != 1:
        raise ValueError("Windows stage and installer must use one signing identity")
    _verify_signoff(
        evidence / "visual-signoff.json",
        {"intended_model_visible", "animation_healthy", "rendering_approved"},
    )
    _verify_signoff(
        evidence / "security-signoff.json",
        {
            "exposed_credentials_revoked",
            "rotated_credentials_stored_securely",
            "mutating_actions_disabled",
        },
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, default=Path("release-evidence"))
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    evidence = verify(args.evidence_root.resolve(), args.tag)
    print(f"verified release evidence: {evidence}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release evidence verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

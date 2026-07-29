"""Require privacy-safe target-machine evidence before publishing a release."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REQUIRED_CHECK_CODES = {
    "voice.": {
        "voice.complete_turn",
        "voice.first_audio_latency",
        "voice.interrupt_terminal",
        "voice.interrupt_latency",
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
    generated_at = _parse_timestamp(report.get("generated_at"), path, "generated_at")
    now = datetime.now(UTC)
    if generated_at > now or generated_at < now - timedelta(days=30):
        raise ValueError(f"{path.name}: acceptance evidence is stale or future-dated")
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
    parsed = _parse_timestamp(signoff.get("observed_at"), path, "observed_at")
    now = datetime.now(UTC)
    if parsed > now or parsed < now - timedelta(days=30):
        raise ValueError(f"{path.name}: observed_at is stale or future-dated")


def verify(evidence_root: Path, tag: str) -> Path:
    if not tag.startswith("v"):
        raise ValueError("release tag must start with v")
    expected_version = tag[1:]
    evidence = evidence_root / tag
    required = {
        "voice-acceptance.json",
        "avatar-acceptance.json",
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

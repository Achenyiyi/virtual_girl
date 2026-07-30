from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.verify_release_evidence import verify as verify_evidence
from scripts.verify_release_version import verify as verify_version


def _write_source(root: Path, project_version: str, runtime_version: str) -> None:
    (root / "companion").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "virtual-companion"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (root / "companion" / "__init__.py").write_text(
        f'__version__ = "{runtime_version}"\n', encoding="utf-8"
    )


def test_release_version_matches_source_tag_and_wheel(tmp_path) -> None:
    _write_source(tmp_path, "1.2.3", "1.2.3")
    wheel = tmp_path / "virtual_companion-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "virtual_companion-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: virtual-companion\nVersion: 1.2.3\n",
        )

    assert verify_version(tmp_path, tag="v1.2.3", wheel=wheel) == "1.2.3"


@pytest.mark.parametrize(
    ("project", "runtime", "tag", "message"),
    [
        ("1.2.3", "1.2.4", "v1.2.3", "runtime version"),
        ("1.2.3", "1.2.3", "v1.2.4", "release tag"),
        ("1.2.3-rc1", "1.2.3-rc1", "v1.2.3-rc1", "stable SemVer"),
    ],
)
def test_release_version_rejects_mismatch(tmp_path, project, runtime, tag, message) -> None:
    _write_source(tmp_path, project, runtime)
    with pytest.raises(ValueError, match=message):
        verify_version(tmp_path, tag=tag)


def _acceptance(prefix: str, version: str) -> dict[str, object]:
    codes = {
        "voice": [
            "voice.complete_turn",
            "voice.first_audio_latency",
            "voice.interrupt_terminal",
            "voice.interrupt_latency",
        ],
        "avatar": [
            "avatar.bridge_health",
            "avatar.model_available",
            "avatar.model_loaded",
            "avatar.renderer_ready",
            "avatar.state_rendered",
            "avatar.frame_presented",
        ],
    }[prefix]
    return {
        "schema_version": 1,
        "app_version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "exit_code": 0,
        "checks": [{"code": code, "passed": True, "message": "passed"} for code in codes],
    }


def _windows_stage(version: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "app_version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "artifact_sha256": {
            "airi_exe": "1" * 64,
            "app_asar": "2" * 64,
            "godot_stage_exe": "3" * 64,
            "managed_avatar": (
                "6c093fb4e37cda43e2bc89df36c9a93d1f42741fbd6ea7dd57a893e32a6fe31d"
            ),
        },
        "authenticode": {
            "airi_exe": {
                "status": "Valid",
                "signer_certificate_sha256": "4" * 64,
                "timestamp_certificate_sha256": "5" * 64,
            },
            "godot_stage_exe": {
                "status": "Valid",
                "signer_certificate_sha256": "6" * 64,
                "timestamp_certificate_sha256": "7" * 64,
            },
        },
        "model_license": {
            "model_id": "managed-nemesia-pajamas",
            "title": "Nemesia_pajamas",
            "author": "awa",
            "source": "embedded-vrm-0.x-meta-and-owner-confirmed-vroid-hub-page",
            "license_url": (
                "https://hub.vroid.com/license?allowed_to_use_user=everyone&"
                "characterization_allowed_user=everyone&corporate_commercial_use=allow&"
                "credit=unnecessary&modification=allow&personal_commercial_use=profit&"
                "redistribution=allow&sexual_expression=allow&version=1&"
                "violent_expression=allow"
            ),
            "corporate_commercial_use": True,
            "personal_commercial_use": True,
            "redistribution": True,
            "modification": True,
            "credit_required": False,
        },
    }


def _write_evidence(root: Path, tag: str) -> Path:
    evidence = root / tag
    evidence.mkdir(parents=True)
    (evidence / "voice-acceptance.json").write_text(
        json.dumps(_acceptance("voice", tag.removeprefix("v"))), encoding="utf-8"
    )
    (evidence / "avatar-acceptance.json").write_text(
        json.dumps(_acceptance("avatar", tag.removeprefix("v"))), encoding="utf-8"
    )
    (evidence / "windows-stage.json").write_text(
        json.dumps(_windows_stage(tag.removeprefix("v"))), encoding="utf-8"
    )
    common = {"reviewer": "release owner", "observed_at": datetime.now(UTC).isoformat()}
    (evidence / "visual-signoff.json").write_text(
        json.dumps(
            common
            | {
                "intended_model_visible": True,
                "animation_healthy": True,
                "rendering_approved": True,
            }
        ),
        encoding="utf-8",
    )
    (evidence / "security-signoff.json").write_text(
        json.dumps(
            common
            | {
                "exposed_credentials_revoked": True,
                "rotated_credentials_stored_securely": True,
                "mutating_actions_disabled": True,
            }
        ),
        encoding="utf-8",
    )
    return evidence


def test_release_evidence_requires_all_external_gates(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    assert verify_evidence(tmp_path, "v1.2.3") == evidence


def test_release_evidence_rejects_failed_or_missing_signoff(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    security = json.loads((evidence / "security-signoff.json").read_text(encoding="utf-8"))
    security["exposed_credentials_revoked"] = False
    (evidence / "security-signoff.json").write_text(json.dumps(security), encoding="utf-8")

    with pytest.raises(ValueError, match="exposed_credentials_revoked"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_unapproved_content_fields(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    voice = json.loads((evidence / "voice-acceptance.json").read_text(encoding="utf-8"))
    voice["transcript"] = "private user content"
    (evidence / "voice-acceptance.json").write_text(json.dumps(voice), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected fields"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_another_version(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.4")
    voice = json.loads((evidence / "voice-acceptance.json").read_text(encoding="utf-8"))
    voice["app_version"] = "1.2.3"
    (evidence / "voice-acceptance.json").write_text(json.dumps(voice), encoding="utf-8")

    with pytest.raises(ValueError, match="app_version"):
        verify_evidence(tmp_path, "v1.2.4")


def test_release_evidence_rejects_windows_stage_for_another_version(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.4")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    stage["app_version"] = "1.2.3"
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="windows-stage.json: app_version"):
        verify_evidence(tmp_path, "v1.2.4")


def test_release_evidence_rejects_stale_acceptance(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    voice = json.loads((evidence / "voice-acceptance.json").read_text(encoding="utf-8"))
    voice["generated_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    (evidence / "voice-acceptance.json").write_text(json.dumps(voice), encoding="utf-8")

    with pytest.raises(ValueError, match="stale or future-dated"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_stale_windows_stage(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    stage["generated_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="windows-stage.json: generated_at"):
        verify_evidence(tmp_path, "v1.2.3")


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-07-29T08:00:00"])
def test_release_evidence_rejects_invalid_timestamp(tmp_path, timestamp) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    visual = json.loads((evidence / "visual-signoff.json").read_text(encoding="utf-8"))
    visual["observed_at"] = timestamp
    (evidence / "visual-signoff.json").write_text(json.dumps(visual), encoding="utf-8")

    with pytest.raises(ValueError, match="observed_at"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_unapproved_signoff_fields(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    visual = json.loads((evidence / "visual-signoff.json").read_text(encoding="utf-8"))
    visual["notes"] = "must not enter public release evidence"
    (evidence / "visual-signoff.json").write_text(json.dumps(visual), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected fields"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_extra_files(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    (evidence / "screenshot.png").write_bytes(b"private visual evidence")

    with pytest.raises(ValueError, match="unexpected entries"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_requires_windows_stage(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    (evidence / "windows-stage.json").unlink()

    with pytest.raises(ValueError, match="windows-stage.json"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_windows_stage_path_field(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    stage["installation_path"] = r"C:\private\user\airi"
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected report fields"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_invalid_windows_artifact_digest(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    stage["artifact_sha256"]["app_asar"] = "NOT-A-DIGEST"
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_sha256.app_asar"):
        verify_evidence(tmp_path, "v1.2.3")


@pytest.mark.parametrize("artifact", ["airi_exe", "godot_stage_exe"])
def test_release_evidence_rejects_invalid_windows_signature(tmp_path, artifact) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    stage["authenticode"][artifact]["status"] = "NotSigned"
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match=rf"authenticode\.{artifact}\.status"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_requires_windows_timestamp_certificate(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage = json.loads((evidence / "windows-stage.json").read_text(encoding="utf-8"))
    del stage["authenticode"]["airi_exe"]["timestamp_certificate_sha256"]
    (evidence / "windows-stage.json").write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="timestamp_certificate_sha256"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_rejects_unapproved_model_digest_or_license(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    stage_path = evidence / "windows-stage.json"
    stage = json.loads(stage_path.read_text(encoding="utf-8"))
    stage["artifact_sha256"]["managed_avatar"] = "8" * 64
    stage_path.write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="managed avatar digest is not approved"):
        verify_evidence(tmp_path, "v1.2.3")

    stage = _windows_stage("1.2.3")
    stage["model_license"]["redistribution"] = False
    stage_path.write_text(json.dumps(stage), encoding="utf-8")

    with pytest.raises(ValueError, match="model_license.redistribution is not approved"):
        verify_evidence(tmp_path, "v1.2.3")


def test_release_evidence_requires_complete_acceptance_checks(tmp_path) -> None:
    evidence = _write_evidence(tmp_path, "v1.2.3")
    voice = json.loads((evidence / "voice-acceptance.json").read_text(encoding="utf-8"))
    voice["checks"] = voice["checks"][:-1]
    (evidence / "voice-acceptance.json").write_text(json.dumps(voice), encoding="utf-8")

    with pytest.raises(ValueError, match="required acceptance checks"):
        verify_evidence(tmp_path, "v1.2.3")

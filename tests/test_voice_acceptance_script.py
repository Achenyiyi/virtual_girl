from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_voice_acceptance.ps1"
EXPECTED_CODES = [
    "voice.complete_turn",
    "voice.first_audio_latency",
    "voice.incremental_playback",
    "voice.pcm_continuity",
    "voice.completed_history",
    "voice.interrupt_terminal",
    "voice.interrupt_latency",
    "voice.interrupted_history",
]


def _report(*, passed: bool = True) -> dict[str, object]:
    checks = [
        {
            "code": code,
            "passed": passed,
            "message": "passed" if passed else "failed",
            "actual_ms": None,
            "target_ms": None,
        }
        for code in EXPECTED_CODES
    ]
    return {
        "schema_version": 1,
        "app_version": "0.1.0",
        "generated_at": "2026-07-31T00:00:00+00:00",
        "exit_code": 0 if passed else 1,
        "passed": passed,
        "checks": checks,
    }


def _fake_python(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-python.cmd"
    fake.write_text(
        "@echo off\n"
        "echo %* | findstr /C:\"--validate-config\" >nul\n"
        "if not errorlevel 1 (\n"
        "  echo Configuration is valid.\n"
        "  exit /b 0\n"
        ")\n"
        "type \"%VOICE_ACCEPTANCE_FAKE_REPORT%\"\n"
        "exit /b %VOICE_ACCEPTANCE_FAKE_EXIT%\n",
        encoding="ascii",
    )
    return fake


def _run_script(tmp_path: Path, report: dict[str, object], *, native_exit: int) -> tuple[int, str]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    report_path = tmp_path / "fake-report.json"
    output_path = tmp_path / "voice-acceptance.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    env = os.environ.copy()
    env["VOICE_ACCEPTANCE_FAKE_REPORT"] = str(report_path)
    env["VOICE_ACCEPTANCE_FAKE_EXIT"] = str(native_exit)
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            "-PythonPath",
            str(_fake_python(tmp_path)),
            "-OutputPath",
            str(output_path),
            "-SkipReadyPrompt",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_voice_acceptance_script_parses_as_powershell() -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    command = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$null,[ref]$errors); if ($errors.Count) { $errors; exit 1 }"
    )
    subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )


def test_voice_acceptance_script_requires_complete_isolated_runtime_arguments() -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            "-IsolatedRuntime",
            "-SkipReadyPrompt",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert "PythonPath" in completed.stderr


def test_voice_acceptance_script_accepts_exact_passing_report(tmp_path: Path) -> None:
    exit_code, output = _run_script(tmp_path, _report(), native_exit=0)

    assert exit_code == 0
    assert "8/8" in output
    stored = json.loads((tmp_path / "voice-acceptance.json").read_text(encoding="utf-8"))
    assert [check["code"] for check in stored["checks"]] == EXPECTED_CODES


def test_voice_acceptance_script_rejects_incomplete_report(tmp_path: Path) -> None:
    report = _report()
    report["checks"] = list(report["checks"])[:-1]

    exit_code, output = _run_script(tmp_path, report, native_exit=0)

    assert exit_code == 1
    assert "8" in output


def test_voice_acceptance_script_preserves_privacy_safe_failure_report(tmp_path: Path) -> None:
    exit_code, output = _run_script(tmp_path, _report(passed=False), native_exit=1)

    assert exit_code == 1
    assert "[FAIL] voice.complete_turn" in output
    assert (tmp_path / "voice-acceptance.json").is_file()


def test_voice_acceptance_script_rejects_unapproved_report_fields(tmp_path: Path) -> None:
    report = _report()
    report["transcript"] = "private content"

    exit_code, output = _run_script(tmp_path, report, native_exit=0)

    assert exit_code == 1
    assert "未批准字段" in output
    assert not (tmp_path / "voice-acceptance.json").exists()

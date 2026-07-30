from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_airi_toolchain_manifest_pins_reproducible_windows_inputs() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "airi-v0.11.3"
        / "toolchain.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["airi_commit"] == "dbf812488829a61cc2e95909e021b215704d066c"
    assert manifest["node_version"] == "24.9.0"
    assert manifest["pnpm_version"] == "10.33.0"
    assert manifest["electron_version"] == "41.2.1"
    assert manifest["electron_builder_version"] == "26.8.1"
    assert manifest["godot_version"] == "4.6.2.stable.mono"
    for digest in manifest["verified_downloads"].values():
        assert len(digest) in {64, 128}
        int(digest, 16)


def test_airi_build_script_pins_pnpm_for_builder_subprocesses() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_airi_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "corepack enable pnpm --install-directory $corepackBin" in script
    assert '$env:PATH = "$corepackBin;$originalPath"' in script
    assert "(pnpm --version).Trim() -ne $PnpmVersion" in script
    assert (
        "pnpm --filter '@proj-airi/stage-tamagotchi...' install --frozen-lockfile"
        in script
    )
    assert '(dotnet --version).Trim().StartsWith("8.0.")' in script
    assert "Godot 4.6.2 stable Mono is required" in script
    assert '[string]$GodotUserPath = ""' in script
    assert "GODOT_USER_HOME" not in script
    assert "$godotAppData = $env:APPDATA" in script
    assert "$godotLocalAppData = $env:LOCALAPPDATA" in script
    assert "APPDATA = $godotAppData" in script
    assert "LOCALAPPDATA = $godotLocalAppData" in script
    assert "$godotProcess = Start-Process @godotProcessArguments" in script
    assert 'WindowStyle = "Hidden"' in script
    assert "Wait = $true" in script
    assert "PassThru = $true" in script
    assert "RedirectStandardOutput = $godotStdout" in script
    assert "RedirectStandardError = $godotStderr" in script
    assert "Start-Process \\" not in script
    assert "'\"Windows Desktop\"'" in script
    assert "if ($godotProcess.ExitCode -ne 0)" in script
    assert '"build\\win\\godot-stage.exe"' in script
    assert "pnpm --filter '@proj-airi/stage-tamagotchi' build:unpack" in script
    assert '"resources\\app.asar"' in script
    assert '"resources\\godot-stage\\godot-stage.exe"' in script


def test_airi_powershell_scripts_parse() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("build_airi_windows.ps1", "verify_airi_windows.ps1"):
        script = root / "scripts" / name
        command = (
            "$tokens=$null; $errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{script}',"
            "[ref]$tokens,[ref]$errors) | Out-Null; "
            "if($errors){$errors | ForEach-Object ToString; exit 1}"
        )
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_airi_verifier_requires_the_pinned_model_license() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_airi_windows.ps1"
    ).read_text(encoding="utf-8")

    assert '"integrations\\airi-v0.11.3\\managed-avatar.json"' in script
    assert "$manifest.schema_version -ne 2" in script
    assert "$manifest.sha256 -ne $ExpectedModelSha256.ToLowerInvariant()" in script
    assert "$permissions.corporate_commercial_use -ne $true" in script
    assert "$permissions.personal_commercial_use -ne $true" in script
    assert "$permissions.redistribution -ne $true" in script
    assert "$permissions.modification -ne $true" in script
    assert "$permissions.credit_required -ne $false" in script
    assert "$embeddedLicense.otherLicenseUrl -ne $license.license_url" in script
    assert '$licenseUri.Host -ne "hub.vroid.com"' in script


def test_airi_verifier_emits_only_release_safe_signed_stage_evidence() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_airi_windows.ps1"
    ).read_text(encoding="utf-8")

    assert '[string]$EvidenceJson = ""' in script
    assert '[string]$AppVersion = ""' in script
    assert "$evidenceRequested -and -not $RequireAuthenticode" in script
    assert '$Signature.Status.ToString() -ne "Valid"' in script
    assert "$null -eq $Signature.SignerCertificate" in script
    assert "$null -eq $Signature.TimeStamperCertificate" in script
    assert "signer_certificate_sha256" in script
    assert "timestamp_certificate_sha256" in script
    assert "artifact_sha256 = [ordered]@{" in script
    assert "model_license = [ordered]@{" in script
    assert "[Text.UTF8Encoding]::new($false)" in script
    assert "[IO.FileMode]::CreateNew" in script


def test_airi_evidence_mode_fails_closed_without_authenticode(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_airi_windows.ps1"
    evidence = tmp_path / "windows-stage.json"
    digest = "0" * 64
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-InstallationPath",
            str(tmp_path / "missing-stage"),
            "-ExpectedExeSha256",
            digest,
            "-ExpectedAppAsarSha256",
            digest,
            "-ExpectedGodotSha256",
            digest,
            "-ModelPath",
            str(tmp_path / "missing-model.vrm"),
            "-ExpectedModelSha256",
            digest,
            "-EvidenceJson",
            str(evidence),
            "-AppVersion",
            "1.2.3",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "EvidenceJson requires RequireAuthenticode" in result.stderr
    assert not evidence.exists()

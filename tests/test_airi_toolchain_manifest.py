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
    assert manifest["dotnet_sdk_version"] == "8.0.206"
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
    assert "pnpm install --frozen-lockfile" in script
    assert "--filter '@proj-airi/stage-tamagotchi...' install" not in script
    assert '$DotnetSdkVersion = "8.0.206"' in script
    assert "$installedDotnetSdks = @(dotnet --list-sdks)" in script
    assert "[IO.FileMode]::CreateNew" in script
    assert 'rollForward = "disable"' in script
    assert "$actualDotnetSdkVersion = (dotnet --version).Trim()" in script
    assert "$actualDotnetSdkVersion -ne $DotnetSdkVersion" in script
    assert "$null -eq $godotVersionOutput" in script
    assert "use the Windows console executable" in script
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


def test_airi_build_script_applies_both_patches_and_rolls_back_partial_failure() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_airi_windows.ps1"
    ).read_text(encoding="utf-8")

    avatar_patch = 'airi-v0.11.3-avatar-bridge.patch'
    desktop_patch = 'airi-v0.11.3-desktop-client.patch'
    assert script.index(avatar_patch) < script.index(desktop_patch)
    assert "foreach ($patch in @($avatarPatch, $desktopPatch))" in script
    assert "Invoke-AiriPatchRollback" in script
    assert "for ($index = $AppliedPatches.Count - 1; $index -ge 0; $index--)" in script
    assert "$appliedPatch = $AppliedPatches[$index]" in script
    assert "Select-Object -Reverse" not in script
    assert "apply --reverse --ignore-space-change --ignore-whitespace" in script
    assert "AIRI patch validation failed" in script
    assert "AIRI patch application failed" in script
    assert '$globalJsonCreated = $false' in script
    open_index = script.index('$globalJsonStream = [IO.File]::Open')
    owned_index = script.index('$globalJsonCreated = $true', open_index)
    write_index = script.index('$globalJsonStream.Write', open_index)
    assert open_index < owned_index < write_index
    assert 'if ($globalJsonCreated -and (Test-Path -LiteralPath $globalJsonPath' in script
    assert 'Remove-Item -LiteralPath $globalJsonPath -Force' in script


def test_airi_ci_checks_both_patches_in_order() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    avatar_patch = "airi-v0.11.3-avatar-bridge.patch"
    desktop_patch = "airi-v0.11.3-desktop-client.patch"
    assert workflow.index(avatar_patch) < workflow.index(desktop_patch)
    assert "foreach ($patch in $patches)" in workflow
    assert "apply --check --ignore-space-change --ignore-whitespace" in workflow
    assert "git -C $airiCheckout apply --ignore-space-change" in workflow


def test_airi_workflow_pins_exact_dotnet_sdk() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "airi-windows.yml"
    ).read_text(encoding="utf-8")

    assert 'dotnet-version: "8.0.206"' in workflow
    assert 'dotnet-version: "8.0.x"' not in workflow
    assert '"Godot_v4.6.2-stable_mono_*_console.exe"' in workflow
    assert "$godotConsoles.Count -ne 1" in workflow
    assert "-GodotPath $godotConsoles[0].FullName" in workflow


def test_ci_secret_scan_excludes_only_pinned_digest_assignments() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    expected = (
        "--exclude-lines '(?i)(\"(?:sha256|[a-z0-9_]+_sha(?:256|512)|airi_commit)\""
        "\\s*:|\\$AiriCommit\\s*=)'"
    )

    assert expected in workflow
    assert "--exclude-files" not in workflow


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

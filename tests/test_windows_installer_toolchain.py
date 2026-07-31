from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.assemble_windows_bundle import validate_runtime_lock, validate_toolchain_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_windows_installer_toolchain_pins_verified_official_inputs() -> None:
    path = ROOT / "packaging" / "windows" / "toolchain.json"
    manifest = validate_toolchain_manifest(path)

    assert manifest["python"] == {
        "version": "3.12.10",
        "implementation": "cpython",
        "architecture": "amd64",
        "embed_url": (
            "https://www.python.org/ftp/python/3.12.10/"
            "python-3.12.10-embed-amd64.zip"
        ),
        "embed_sha256": (
            "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
        ),
        "pth_file": "python312._pth",
    }
    assert manifest["inno_setup"] == {
        "version": "7.0.2",
        "architecture": "x64",
        "installer_url": (
            "https://github.com/jrsoftware/issrc/releases/download/"
            "is-7_0_2/innosetup-7.0.2-x64.exe"
        ),
        "installer_sha256": (
            "5ad54ca3def786f8f4212552e54cc6d8d61329e2d24a1cfee0571d42c2684ff1"
        ),
        "publisher": "Pyrsys B.V.",
        "compiler_relative_path": "ISCC.exe",
        "license_url": "https://github.com/jrsoftware/issrc/blob/is-7_0_2/license.txt",
        "license_allows_commercial_use": True,
        "commercial_license_request_url": "https://jrsoftware.org/isorder.php",
    }
    assert manifest["signing"] == {
        "file_digest_algorithm": "SHA256",
        "timestamp_digest_algorithm": "SHA256",
        "timestamp_url": "https://timestamp.digicert.com",
        "required_artifacts": [
            "airi/airi.exe",
            "airi/resources/godot-stage/godot-stage.exe",
            "installer",
            "uninstaller",
        ],
    }


def test_runtime_lock_is_a_runtime_only_subset_of_the_full_lock() -> None:
    packages = validate_runtime_lock(
        ROOT / "requirements-runtime.lock", ROOT / "requirements.lock"
    )

    assert "faster-whisper" in packages
    assert "jieba" in packages
    assert "sounddevice" in packages
    assert "pytest" not in packages
    assert "build" not in packages
    assert "pip-audit" not in packages

    assembler = (ROOT / "scripts" / "assemble_windows_bundle.py").read_text(
        encoding="utf-8"
    )
    assert 'RUNTIME_SDIST_ALLOWLIST = {"jieba"}' in assembler
    assert '"--only-binary=:all:"' in assembler
    assert 'f"--no-binary={\',\'.join(sorted(RUNTIME_SDIST_ALLOWLIST))}"' in assembler


def test_installer_is_per_user_x64_and_provisions_only_the_local_bridge_token() -> None:
    script = (ROOT / "packaging" / "windows" / "installer.iss").read_text(
        encoding="utf-8"
    )

    assert "PrivilegesRequired=lowest" in script
    assert "ArchitecturesAllowed=x64compatible" in script
    assert "DefaultDirName={localappdata}\\Programs\\Virtual Companion" in script
    assert "--provision-avatar-token" in script
    assert "-I -s -B -m companion" in script
    assert "SkipAvatarToken" in script
    assert "SignTool={#SignToolName}" in script
    assert "SignedUninstaller=yes" in script
    assert "DEEPSEEK" not in script
    assert "AZURE_SPEECH" not in script


def test_installer_scripts_parse_and_require_signing_without_a_bypass() -> None:
    for name in ("build_windows_installer.ps1", "verify_windows_installer.ps1"):
        script = ROOT / "scripts" / name
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

    build = (ROOT / "scripts" / "build_windows_installer.ps1").read_text(
        encoding="utf-8"
    )
    verify = (ROOT / "scripts" / "verify_windows_installer.ps1").read_text(
        encoding="utf-8"
    )
    assert "SigningCertificateThumbprint" in build
    assert "--untracked-files=all" in build
    assert "Select-Object -First 1" in build
    assert "Installer builds require CPython" in build
    assert "Code Signing EKU" in build
    assert "/tr $toolchain.signing.timestamp_url" in build
    assert '"/S${innoSignToolName}=$innoSignCommand"' in build
    assert '"/DSignToolName=$innoSignToolName"' in build
    assert 'Assert-ValidSignature $compiledInstaller "Windows installer"' in build
    assert "-StageEvidenceJson $stageEvidence" in build
    assert "AllowUnsigned" not in build
    assert 'Status.ToString() -ne "Valid"' in verify
    assert "$null -eq $signature.TimeStamperCertificate" in verify
    assert "windows_stage_evidence_sha256" in verify
    assert "import companion, ctranslate2, faster_whisper, numpy, sounddevice" in verify
    assert "--allow-inno-uninstaller" in verify
    assert 'Assert-ValidSignature $uninstallers[0].FullName' in verify
    assert "uninstaller_authenticode" in verify
    assert "Silent uninstaller smoke test" in verify


def test_github_publication_is_source_only() -> None:
    workflows = ROOT / ".github" / "workflows"
    release_policy = (ROOT / "docs" / "release_process.md").read_text(encoding="utf-8")

    assert not (workflows / "release.yml").exists()
    assert "GitHub Releases are not created" in release_policy
    assert "Authenticode" in release_policy
    assert "does not weaken" in release_policy

    forbidden_publishers = (
        "actions/upload-artifact",
        "actions/create-release",
        "actions/upload-release-asset",
        "actions/attest",
        "attestations: write",
        "contents: write",
        "gh release create",
        "gh release upload",
        "gh release edit",
        "id-token: write",
        "ncipollo/release-action",
        "packages: write",
        "pypa/gh-action-pypi-publish",
        "softprops/action-gh-release",
        "twine upload",
    )
    for path in (*workflows.glob("*.yml"), *workflows.glob("*.yaml")):
        workflow = path.read_text(encoding="utf-8")
        for publisher in forbidden_publishers:
            assert publisher not in workflow


def test_toolchain_manifest_has_no_unreviewed_fields() -> None:
    manifest = json.loads(
        (ROOT / "packaging" / "windows" / "toolchain.json").read_text(
            encoding="utf-8"
        )
    )

    assert set(manifest) == {"schema_version", "target", "python", "inno_setup", "signing"}

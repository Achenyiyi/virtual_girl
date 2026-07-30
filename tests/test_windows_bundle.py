from __future__ import annotations

import json
import struct
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from scripts.assemble_windows_bundle import assemble_bundle, validate_runtime_lock
from scripts.verify_windows_bundle import verify_bundle

ROOT = Path(__file__).resolve().parents[1]


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_model_and_manifest(root: Path) -> tuple[Path, Path, dict[str, object]]:
    license_value: dict[str, object] = {
        "source": "embedded-vrm-0.x-meta-and-owner-confirmed-vroid-hub-page",
        "title": "Fixture avatar",
        "author": "Fixture author",
        "allowed_user_name": "Everyone",
        "commercial_usage_name": "Allow",
        "license_name": "Other",
        "license_url": "https://hub.vroid.com/license?redistribution=allow",
        "permissions": {
            "corporate_commercial_use": True,
            "personal_commercial_use": True,
            "redistribution": True,
            "modification": True,
            "credit_required": False,
        },
        "reviewed_at": "2026-07-30",
    }
    embedded = {
        "title": license_value["title"],
        "author": license_value["author"],
        "allowedUserName": license_value["allowed_user_name"],
        "commercialUssageName": license_value["commercial_usage_name"],
        "licenseName": license_value["license_name"],
        "otherPermissionUrl": license_value["license_url"],
        "otherLicenseUrl": license_value["license_url"],
    }
    payload = json.dumps(
        {"extensions": {"VRM": {"meta": embedded}}}, separators=(",", ":")
    ).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    model = root / "avatar.vrm"
    model.write_bytes(
        struct.pack(
            "<4sIIII",
            b"glTF",
            2,
            20 + len(payload),
            len(payload),
            int.from_bytes(b"JSON", "little"),
        )
        + payload
    )
    manifest: dict[str, object] = {
        "schema_version": 2,
        "model_id": "fixture-avatar",
        "display_name": "Fixture avatar",
        "relative_path": "model/avatar.vrm",
        "size_bytes": model.stat().st_size,
        "sha256": _digest(model),
        "container": "glb-2.0",
        "vrm_spec": "0.x",
        "license": license_value,
        "repository_policy": "local-user-asset-not-committed",
    }
    manifest_path = root / "managed-avatar.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return model, manifest_path, manifest


def _write_stage(root: Path) -> Path:
    stage = root / "stage"
    (stage / "resources" / "godot-stage").mkdir(parents=True)
    (stage / "airi.exe").write_bytes(b"signed airi fixture")
    (stage / "resources" / "app.asar").write_bytes(b"renderer fixture")
    (stage / "resources" / "godot-stage" / "godot-stage.exe").write_bytes(
        b"signed godot fixture"
    )
    return stage


def _write_stage_evidence(
    root: Path,
    stage: Path,
    model: Path,
    manifest: dict[str, object],
) -> Path:
    license_value = manifest["license"]
    assert isinstance(license_value, dict)
    permissions = license_value["permissions"]
    assert isinstance(permissions, dict)
    evidence = {
        "schema_version": 1,
        "app_version": "1.2.3",
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "artifact_sha256": {
            "airi_exe": _digest(stage / "airi.exe"),
            "app_asar": _digest(stage / "resources" / "app.asar"),
            "godot_stage_exe": _digest(
                stage / "resources" / "godot-stage" / "godot-stage.exe"
            ),
            "managed_avatar": _digest(model),
        },
        "authenticode": {
            name: {
                "status": "Valid",
                "signer_certificate_sha256": "a" * 64,
                "timestamp_certificate_sha256": "b" * 64,
            }
            for name in ("airi_exe", "godot_stage_exe")
        },
        "model_license": {
            "model_id": manifest["model_id"],
            "title": license_value["title"],
            "author": license_value["author"],
            "source": license_value["source"],
            "license_url": license_value["license_url"],
            **permissions,
        },
    }
    path = root / "windows-stage.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _write_python_embed_and_toolchain(root: Path) -> tuple[Path, Path]:
    archive = root / "python-embed.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("python.exe", b"fixture")
        output.writestr("pythonw.exe", b"fixture")
        output.writestr("python312.zip", b"fixture")
        output.writestr("python312._pth", "python312.zip\n.\n#import site\n")
    toolchain = {
        "schema_version": 1,
        "target": {
            "os": "windows",
            "architecture": "x86_64",
            "minimum_version": "10.0.19041",
        },
        "python": {
            "version": "3.12.10",
            "implementation": "cpython",
            "architecture": "amd64",
            "embed_url": "https://www.python.org/fixture.zip",
            "embed_sha256": _digest(archive),
            "pth_file": "python312._pth",
        },
        "inno_setup": {
            "version": "7.0.2",
            "architecture": "x64",
            "installer_url": "https://github.com/fixture.exe",
            "installer_sha256": "c" * 64,
            "publisher": "Pyrsys B.V.",
            "compiler_relative_path": "ISCC.exe",
            "license_url": "https://github.com/jrsoftware/issrc/blob/is-7_0_2/license.txt",
            "license_allows_commercial_use": True,
            "commercial_license_request_url": "https://jrsoftware.org/isorder.php",
        },
        "signing": {
            "file_digest_algorithm": "SHA256",
            "timestamp_digest_algorithm": "SHA256",
            "timestamp_url": "https://timestamp.digicert.com",
            "required_artifacts": [
                "airi/airi.exe",
                "airi/resources/godot-stage/godot-stage.exe",
                "installer",
                "uninstaller",
            ],
        },
    }
    path = root / "toolchain.json"
    path.write_text(json.dumps(toolchain), encoding="utf-8")
    return archive, path


def _write_locks(root: Path) -> tuple[Path, Path]:
    entries = {
        "faster-whisper": "1.2.1",
        "numpy": "2.5.1",
        "pydantic": "2.13.4",
        "pyyaml": "6.0.3",
        "sounddevice": "0.5.5",
    }
    text = "".join(
        f"{name}=={version} \\\n    --hash=sha256:{index:064x}\n"
        for index, (name, version) in enumerate(entries.items(), start=1)
    )
    runtime = root / "requirements-runtime.lock"
    full = root / "requirements.lock"
    runtime.write_text(text, encoding="utf-8")
    full.write_text(text, encoding="utf-8")
    return runtime, full


def _write_wheel(root: Path) -> Path:
    wheel = root / "virtual_companion-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "virtual_companion-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: virtual-companion\nVersion: 1.2.3\n",
        )
    return wheel


def _assemble_fixture(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model, model_manifest, manifest = _write_model_and_manifest(tmp_path)
    stage = _write_stage(tmp_path)
    stage_evidence = _write_stage_evidence(tmp_path, stage, model, manifest)
    python_embed, toolchain = _write_python_embed_and_toolchain(tmp_path)
    runtime_lock, full_lock = _write_locks(tmp_path)
    wheel = _write_wheel(tmp_path)

    def install_fixture(
        _python: Path, _runtime_lock: Path, _wheel: Path, site_packages: Path
    ) -> None:
        package = site_packages / "companion"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="ascii")
        certifi = site_packages / "certifi"
        certifi.mkdir()
        (certifi / "cacert.pem").write_text("public CA bundle\n", encoding="ascii")
        grpc = site_packages / "grpc" / "_cython" / "_credentials"
        grpc.mkdir(parents=True)
        (grpc / "roots.pem").write_text("public gRPC roots\n", encoding="ascii")

    output = tmp_path / "bundle"
    return assemble_bundle(
        stage=stage,
        model=model,
        wheel=wheel,
        python_embed=python_embed,
        runtime_lock=runtime_lock,
        full_lock=full_lock,
        stage_evidence_path=stage_evidence,
        model_manifest_path=model_manifest,
        toolchain_path=toolchain,
        default_config=ROOT / "companion" / "resources" / "default.yaml",
        project_license=ROOT / "LICENSE",
        third_party_assets=ROOT / "docs" / "third_party_assets.md",
        output=output,
        app_version="1.2.3",
        source_commit="d" * 40,
        dependency_installer=install_fixture,
    )


def test_bundle_assembly_pins_assets_and_generates_secret_free_config(tmp_path) -> None:
    bundle = _assemble_fixture(tmp_path)

    manifest = verify_bundle(
        bundle, expected_version="1.2.3", expected_commit="d" * 40
    )
    config = yaml.safe_load(
        (bundle / "config" / "production.yaml").read_text(encoding="utf-8")
    )

    assert manifest["target"] == "windows-x86_64"
    assert config["runtime"]["data_root"] == "user_local"
    assert config["identity"]["avatar_model_id"] == "fixture-avatar"
    assert config["providers"]["avatar"]["enabled"] is True
    launch = config["providers"]["avatar"]["launch"]
    assert launch["executable_path"] == "../airi/airi.exe"
    assert launch["model_path"] == "../model/managed-avatar.vrm"
    assert "api_key" not in config["providers"]["llm"]["cloud"]
    assert (bundle / "runtime" / "python312._pth").read_text(encoding="ascii") == (
        "python312.zip\n.\nLib/site-packages\nimport site\n"
    )
    assert 'python.exe" -I -s -B -m companion' in (
        bundle / "launch-companion.cmd"
    ).read_text(encoding="ascii")


def test_bundle_verifier_rejects_tampering_and_unexpected_files(tmp_path) -> None:
    bundle = _assemble_fixture(tmp_path)
    launcher = bundle / "launch-companion.cmd"
    launcher.write_bytes(launcher.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_bundle(bundle)

    bundle = _assemble_fixture(tmp_path / "second")
    (bundle / "credential.key").write_text("private", encoding="ascii")
    with pytest.raises(ValueError, match="file set differs"):
        verify_bundle(bundle)


def test_bundle_verifier_links_manifest_inputs_to_packaged_files(tmp_path) -> None:
    bundle = _assemble_fixture(tmp_path)
    manifest_path = bundle / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["inputs"]["managed_avatar_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")

    with pytest.raises(ValueError, match="managed_avatar_sha256 does not match"):
        verify_bundle(bundle)


def test_bundle_verifier_requires_the_pinned_python_version(tmp_path) -> None:
    bundle = _assemble_fixture(tmp_path)
    manifest_path = bundle / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["python_version"] = "3.13.0"
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")

    with pytest.raises(ValueError, match="Python version"):
        verify_bundle(bundle)


def test_bundle_verifier_rejects_unapproved_pem_files(tmp_path) -> None:
    bundle = _assemble_fixture(tmp_path)
    private_pem = bundle / "runtime" / "Lib" / "site-packages" / "private.pem"
    private_pem.write_text("private material", encoding="ascii")
    manifest_path = bundle / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["files"].append(
        {
            "path": private_pem.relative_to(bundle).as_posix(),
            "size_bytes": private_pem.stat().st_size,
            "sha256": _digest(private_pem),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")

    with pytest.raises(ValueError, match="private runtime data"):
        verify_bundle(bundle)


def test_runtime_lock_rejects_development_tools(tmp_path) -> None:
    runtime, full = _write_locks(tmp_path)
    extra = "pytest==9.1.1 \\\n    --hash=sha256:" + "f" * 64 + "\n"
    runtime.write_text(runtime.read_text(encoding="utf-8") + extra, encoding="utf-8")
    full.write_text(full.read_text(encoding="utf-8") + extra, encoding="utf-8")

    with pytest.raises(ValueError, match="non-runtime tools"):
        validate_runtime_lock(runtime, full)


def test_bundle_assembly_rejects_stage_hash_drift(tmp_path) -> None:
    model, model_manifest, manifest = _write_model_and_manifest(tmp_path)
    stage = _write_stage(tmp_path)
    stage_evidence = _write_stage_evidence(tmp_path, stage, model, manifest)
    (stage / "airi.exe").write_bytes(b"changed after signing")
    python_embed, toolchain = _write_python_embed_and_toolchain(tmp_path)
    runtime_lock, full_lock = _write_locks(tmp_path)

    with pytest.raises(ValueError, match="digest mismatch for airi_exe"):
        assemble_bundle(
            stage=stage,
            model=model,
            wheel=_write_wheel(tmp_path),
            python_embed=python_embed,
            runtime_lock=runtime_lock,
            full_lock=full_lock,
            stage_evidence_path=stage_evidence,
            model_manifest_path=model_manifest,
            toolchain_path=toolchain,
            default_config=ROOT / "companion" / "resources" / "default.yaml",
            project_license=ROOT / "LICENSE",
            third_party_assets=ROOT / "docs" / "third_party_assets.md",
            output=tmp_path / "bundle",
            app_version="1.2.3",
            source_commit="d" * 40,
        )

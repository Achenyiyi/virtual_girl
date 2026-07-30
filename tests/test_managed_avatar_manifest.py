from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "integrations" / "airi-v0.11.3" / "managed-avatar.json"


def test_managed_avatar_manifest_pins_the_user_model_without_bundling_it() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest == {
        "schema_version": 2,
        "model_id": "managed-nemesia-pajamas",
        "display_name": "Nemesia pajamas",
        "relative_path": "model/8496491754682859078.vrm",
        "size_bytes": 30_688_684,
        "sha256": "6c093fb4e37cda43e2bc89df36c9a93d1f42741fbd6ea7dd57a893e32a6fe31d",
        "container": "glb-2.0",
        "vrm_spec": "0.x",
        "license": {
            "source": "embedded-vrm-0.x-meta-and-owner-confirmed-vroid-hub-page",
            "title": "Nemesia_pajamas",
            "author": "awa",
            "allowed_user_name": "Everyone",
            "commercial_usage_name": "Allow",
            "license_name": "Other",
            "license_url": (
                "https://hub.vroid.com/license?allowed_to_use_user=everyone&"
                "characterization_allowed_user=everyone&corporate_commercial_use=allow&"
                "credit=unnecessary&modification=allow&personal_commercial_use=profit&"
                "redistribution=allow&sexual_expression=allow&version=1&"
                "violent_expression=allow"
            ),
            "permissions": {
                "corporate_commercial_use": True,
                "personal_commercial_use": True,
                "redistribution": True,
                "modification": True,
                "credit_required": False,
            },
            "reviewed_at": "2026-07-30",
        },
        "repository_policy": "local-user-asset-not-committed",
    }
    model = ROOT / manifest["relative_path"]
    assert not model.is_relative_to(ROOT / "companion")
    assert not model.is_relative_to(ROOT / "integrations")

    license_info = manifest["license"]
    license_url = urlparse(license_info["license_url"])
    assert (license_url.scheme, license_url.netloc, license_url.path) == (
        "https",
        "hub.vroid.com",
        "/license",
    )
    assert parse_qs(license_url.query) == {
        "allowed_to_use_user": ["everyone"],
        "characterization_allowed_user": ["everyone"],
        "corporate_commercial_use": ["allow"],
        "credit": ["unnecessary"],
        "modification": ["allow"],
        "personal_commercial_use": ["profit"],
        "redistribution": ["allow"],
        "sexual_expression": ["allow"],
        "version": ["1"],
        "violent_expression": ["allow"],
    }


def test_local_managed_avatar_matches_manifest_when_present() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    model = ROOT / manifest["relative_path"]
    if not model.exists():
        return

    assert model.stat().st_size == manifest["size_bytes"]
    with model.open("rb") as stream:
        magic, version, length, json_length, chunk_type = struct.unpack(
            "<4sIIII", stream.read(20)
        )
        document = json.loads(stream.read(json_length).decode("utf-8").rstrip("\x00 \t\r\n"))
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256")
    assert (magic, version, length) == (b"glTF", 2, manifest["size_bytes"])
    assert chunk_type == int.from_bytes(b"JSON", "little")
    assert digest.hexdigest() == manifest["sha256"]

    embedded = document["extensions"]["VRM"]["meta"]
    license_info = manifest["license"]
    assert embedded["title"] == license_info["title"]
    assert embedded["author"] == license_info["author"]
    assert embedded["allowedUserName"] == license_info["allowed_user_name"]
    assert embedded["commercialUssageName"] == license_info["commercial_usage_name"]
    assert embedded["licenseName"] == license_info["license_name"]
    assert embedded["otherPermissionUrl"] == license_info["license_url"]
    assert embedded["otherLicenseUrl"] == license_info["license_url"]

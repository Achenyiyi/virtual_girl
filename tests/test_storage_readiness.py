"""Deployment boundary tests for local writable runtime storage."""

from __future__ import annotations

import shutil
from argparse import Namespace
from collections import namedtuple

import pytest

from companion.__main__ import CompanionApp, async_main
from companion.security.storage_readiness import check_runtime_storage


def test_storage_probe_writes_flushes_and_removes_temporary_file(tmp_path) -> None:
    target = tmp_path / "nested" / "memory.db"

    result = check_runtime_storage(target, minimum_free_bytes=1)

    assert result.path == target.resolve()
    assert result.parent == target.parent.resolve()
    assert result.free_bytes > 0
    assert not target.parent.exists()
    assert list(tmp_path.glob(".virtual-companion-write-*.tmp")) == []


def test_existing_runtime_file_must_be_openable_for_writes(tmp_path, monkeypatch) -> None:
    target = tmp_path / "memory.db"
    target.write_bytes(b"sqlite-placeholder")
    real_open = __import__("os").open

    def deny_target(path, flags, mode=0o777):
        if str(path) == str(target.resolve()):
            raise PermissionError("write denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr("companion.security.storage_readiness.os.open", deny_target)

    with pytest.raises(PermissionError):
        check_runtime_storage(target, minimum_free_bytes=1)


def test_low_free_space_fails_before_write_probe(tmp_path, monkeypatch) -> None:
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "companion.security.storage_readiness.shutil.disk_usage",
        lambda _path: Usage(100, 99, 1),
    )

    with pytest.raises(OSError, match="insufficient free space"):
        check_runtime_storage(tmp_path / "memory.db", minimum_free_bytes=2)

    assert list(tmp_path.glob(".virtual-companion-write-*.tmp")) == []


def test_remote_volume_is_rejected_before_disk_or_write_access(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "companion.security.storage_readiness._is_remote_path", lambda _path: True
    )

    def unexpected_disk_usage(_path) -> shutil._ntuple_diskusage:
        raise AssertionError("remote storage must fail before disk access")

    monkeypatch.setattr(
        "companion.security.storage_readiness.shutil.disk_usage", unexpected_disk_usage
    )

    with pytest.raises(OSError, match="local Windows volume"):
        check_runtime_storage(tmp_path / "memory.db", minimum_free_bytes=1)


def test_invalid_free_space_threshold_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        check_runtime_storage(tmp_path / "memory.db", minimum_free_bytes=0)


@pytest.mark.asyncio
async def test_runtime_storage_failure_exits_before_provider_construction(
    tmp_path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "companion.yaml"
    config_path.write_text("providers:\n  memory:\n    type: sqlite\n", encoding="utf-8")

    def fail_storage(_path):
        raise OSError("insufficient free space")

    def unexpected_app(_config) -> CompanionApp:
        raise AssertionError("providers must not be constructed without ready storage")

    monkeypatch.setattr("companion.__main__.check_runtime_storage", fail_storage)
    monkeypatch.setattr("companion.__main__.CompanionApp", unexpected_app)
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=False,
        accept_avatar=False,
        accept_avatar_json=False,
        backup_memory=None,
        verify_memory_backup=None,
        overwrite_backup=False,
        log_level=None,
        voice_input=False,
        voice=False,
        once=None,
    )

    assert await async_main(args) == 1

    assert "Runtime storage unavailable: OSError" in capsys.readouterr().err

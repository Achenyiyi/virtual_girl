"""Process-boundary tests for the Windows single-instance guard."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from argparse import Namespace

import pytest

from companion.__main__ import CompanionApp, async_main
from companion.security.single_instance import (
    InstanceAlreadyRunningError,
    SingleInstanceGuard,
)


def test_same_memory_store_cannot_be_owned_twice(tmp_path) -> None:
    first = SingleInstanceGuard.for_memory_path(str(tmp_path / "memory.db"))
    second = SingleInstanceGuard.for_memory_path(str(tmp_path / "." / "memory.db"))

    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_same_memory_store_cannot_be_owned_by_another_process(tmp_path) -> None:
    memory_path = tmp_path / "cross-process.db"
    owner_code = textwrap.dedent(
        """
        import sys
        from companion.security.single_instance import SingleInstanceGuard

        guard = SingleInstanceGuard.for_memory_path(sys.argv[1])
        guard.acquire()
        print("owned", flush=True)
        sys.stdin.readline()
        guard.release()
        """
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(memory_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "owned"
        contender = SingleInstanceGuard.for_memory_path(str(memory_path))
        with pytest.raises(InstanceAlreadyRunningError):
            contender.acquire()
    finally:
        if owner.stdin is not None:
            owner.stdin.write("release\n")
            owner.stdin.flush()
        stdout, stderr = owner.communicate(timeout=5)
        assert owner.returncode == 0, (stdout, stderr)


def test_different_memory_stores_have_independent_guards(tmp_path) -> None:
    first = SingleInstanceGuard.for_memory_path(str(tmp_path / "first.db"))
    second = SingleInstanceGuard.for_memory_path(str(tmp_path / "second.db"))

    first.acquire()
    second.acquire()
    second.release()
    first.release()


def test_guard_acquire_and_release_are_idempotent(tmp_path) -> None:
    guard = SingleInstanceGuard.for_memory_path(str(tmp_path / "memory.db"))

    guard.acquire()
    guard.acquire()
    guard.release()
    guard.release()


def test_mutex_name_does_not_disclose_memory_path(tmp_path) -> None:
    memory_path = str(tmp_path / "private-user-name" / "memory.db")

    guard = SingleInstanceGuard.for_memory_path(memory_path)

    assert guard.name.startswith("Local\\VirtualCompanion-")
    assert "private-user-name" not in guard.name
    assert len(guard.name) < 80


@pytest.mark.asyncio
async def test_second_runtime_exits_before_constructing_providers(
    tmp_path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "companion.yaml"
    memory_path = tmp_path / "memory.db"
    config_path.write_text(
        f"""providers:
  llm:
    type: cloud
    cloud:
      provider: openai_compatible
      model: test-model
      api_key_env: TEST_SINGLE_INSTANCE_KEY
      base_url: https://example.invalid/v1/chat/completions
  memory:
    type: sqlite
    db_path: {memory_path.as_posix()}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_SINGLE_INSTANCE_KEY", "x" * 32)
    owner = SingleInstanceGuard.for_memory_path(str(memory_path))
    owner.acquire()

    def unexpected_app(_config) -> CompanionApp:
        raise AssertionError("providers must not be constructed by a second runtime")

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
    try:
        assert await async_main(args) == 1
    finally:
        owner.release()

    assert "already using this memory store" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_voice_json_instance_conflict_preserves_machine_output(
    tmp_path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "companion.yaml"
    memory_path = tmp_path / "memory.db"
    config_path.write_text(
        f"""providers:
  memory:
    type: sqlite
    db_path: {memory_path.as_posix()}
""",
        encoding="utf-8",
    )
    owner = SingleInstanceGuard.for_memory_path(str(memory_path))
    owner.acquire()
    args = Namespace(
        config=config_path,
        doctor=False,
        doctor_online=False,
        doctor_json=False,
        doctor_voice_hardware=False,
        accept_voice=False,
        accept_voice_json=True,
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
    try:
        assert await async_main(args) == 1
    finally:
        owner.release()

    output = capsys.readouterr()
    assert '"code": "voice.runtime_instance"' in output.out
    assert output.err == ""

"""Least-privilege access to secrets stored by Windows Credential Manager."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_CRED_MAX_BLOB_BYTES = 2560
_ERROR_NOT_FOUND = 1168
_ERROR_ALREADY_EXISTS = 183


@dataclass(frozen=True)
class ResolvedSecret:
    value: str
    source: str


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def resolve_secret(*, env_name: str = "", credential_target: str = "") -> ResolvedSecret:
    """Resolve an environment override, then a generic Windows credential."""
    _validate_reference(env_name=env_name, credential_target=credential_target)
    if env_name:
        value = os.environ.get(env_name, "")
        if value:
            return ResolvedSecret(value=value, source=f"environment variable {env_name}")
    if credential_target:
        value = read_windows_credential(credential_target)
        if value:
            return ResolvedSecret(
                value=value, source=f"Windows Credential Manager target {credential_target}"
            )
    return ResolvedSecret(value="", source="")


def configured_secret_sources(*, env_name: str = "", credential_target: str = "") -> str:
    """Describe configured lookup locations without reading or exposing a secret."""
    _validate_reference(env_name=env_name, credential_target=credential_target)
    sources = []
    if env_name:
        sources.append(f"environment variable {env_name}")
    if credential_target:
        sources.append(f"Windows Credential Manager target {credential_target}")
    return " or ".join(sources) if sources else "no credential source"


def read_windows_credential(target: str) -> str:
    """Read a generic credential blob; missing credentials return an empty string."""
    _validate_reference(credential_target=target)
    if sys.platform != "win32":
        return ""
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential_pointer = ctypes.POINTER(_CredentialW)()
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None
    if not cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
        return ""
    try:
        credential = credential_pointer.contents
        if not credential.CredentialBlob or credential.CredentialBlobSize == 0:
            return ""
        blob = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        return _decode_credential_blob(blob)
    finally:
        cred_free(credential_pointer)


def write_windows_credential(
    target: str,
    value: str,
    *,
    username: str = "VirtualCompanion",
) -> None:
    """Write a non-empty UTF-16 Generic Credential without logging its value."""
    _validate_reference(credential_target=target)
    if not value:
        raise ValueError("Windows credential value must not be empty")
    _validate_username(username)
    blob = value.encode("utf-16-le")
    if len(blob) > _CRED_MAX_BLOB_BYTES:
        raise ValueError("Windows credential value is too large")
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is unavailable on this platform")

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _CredentialW(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment="Virtual Companion managed credential",
        LastWritten=wintypes.FILETIME(),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username,
    )
    if not cred_write(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def write_windows_credential_if_missing(
    target: str,
    value: str,
    *,
    username: str = "VirtualCompanion",
) -> bool:
    """Create a credential once without replacing an existing value."""
    _validate_reference(credential_target=target)
    if not value:
        raise ValueError("Windows credential value must not be empty")
    _validate_username(username)
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is unavailable on this platform")

    digest = hashlib.sha256(target.casefold().encode("utf-8")).hexdigest()[:32]
    mutex_name = f"Global\\VirtualCompanion-Credential-{digest}"
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = [wintypes.HANDLE]
    release_mutex.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create_mutex(None, True, mutex_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_provisioning = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    try:
        if already_provisioning:
            raise OSError("Another process is provisioning this Windows credential")
        if windows_credential_exists(target):
            return False
        write_windows_credential(target, value, username=username)
        return True
    finally:
        active_exception = sys.exception()
        release_error = 0
        if not already_provisioning and not release_mutex(handle):
            release_error = ctypes.get_last_error()
        close_error = 0
        if not close_handle(handle):
            close_error = ctypes.get_last_error()
        if active_exception is None:
            if release_error:
                raise ctypes.WinError(release_error)
            if close_error:
                raise ctypes.WinError(close_error)


def windows_credential_exists(target: str) -> bool:
    """Check an exact Generic Credential target without enumerating the vault."""
    _validate_reference(credential_target=target)
    if sys.platform != "win32":
        return False
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential_pointer = ctypes.POINTER(_CredentialW)()
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None
    if cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
        cred_free(credential_pointer)
        return True
    error = ctypes.get_last_error()
    if error == _ERROR_NOT_FOUND:
        return False
    raise ctypes.WinError(error)


def _decode_credential_blob(blob: bytes) -> str:
    try:
        return blob.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return ""


def _validate_reference(*, env_name: str = "", credential_target: str = "") -> None:
    if env_name and not _ENV_NAME.fullmatch(env_name):
        raise ValueError("credential environment variable name is invalid")
    if credential_target and (
        len(credential_target) > 256
        or credential_target != credential_target.strip()
        or any(ord(character) < 32 for character in credential_target)
    ):
        raise ValueError("Windows credential target is invalid")


def _validate_username(username: str) -> None:
    if not username or len(username) > 256 or any(ord(character) < 32 for character in username):
        raise ValueError("Windows credential username is invalid")

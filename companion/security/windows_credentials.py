"""Least-privilege access to secrets stored by Windows Credential Manager."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys
from ctypes import wintypes
from dataclasses import dataclass

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_MAX_CREDENTIAL_BLOB_BYTES = 512


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


def write_windows_credential(target: str, value: str, *, overwrite: bool = False) -> None:
    """Write one generic credential without exposing its value to a child process."""
    _validate_reference(credential_target=target)
    blob = _encode_credential_blob(value)
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is unavailable on this platform")
    if not overwrite and read_windows_credential(target):
        raise FileExistsError(f"Windows credential already exists: {target}")

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _CredentialW()
    credential.Flags = 0
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "Virtual Companion managed credential"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        blob_buffer, ctypes.POINTER(ctypes.c_ubyte)
    )
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.AttributeCount = 0
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = ""
    try:
        if not cred_write(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.memset(ctypes.addressof(blob_buffer), 0, len(blob))

    if read_windows_credential(target) != value:
        raise OSError("Windows credential write verification failed")


def provision_avatar_bridge_credential(target: str, *, overwrite: bool = False) -> None:
    """Generate and persist a local bridge token without returning or printing it."""
    token = secrets.token_urlsafe(32)
    write_windows_credential(target, token, overwrite=overwrite)


def _decode_credential_blob(blob: bytes) -> str:
    try:
        return blob.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return ""


def _encode_credential_blob(value: str) -> bytes:
    if not value or "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError("credential value is invalid")
    blob = value.encode("utf-16-le")
    if len(blob) > _MAX_CREDENTIAL_BLOB_BYTES:
        raise ValueError("credential value is too long")
    return blob


def _validate_reference(*, env_name: str = "", credential_target: str = "") -> None:
    if env_name and not _ENV_NAME.fullmatch(env_name):
        raise ValueError("credential environment variable name is invalid")
    if credential_target and (
        len(credential_target) > 256
        or credential_target != credential_target.strip()
        or any(ord(character) < 32 for character in credential_target)
    ):
        raise ValueError("Windows credential target is invalid")

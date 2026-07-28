"""Least-privilege access to secrets stored by Windows Credential Manager."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CRED_TYPE_GENERIC = 1


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

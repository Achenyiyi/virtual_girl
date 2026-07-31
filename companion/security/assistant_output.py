"""Sanitize assistant-visible text before it reaches UI, memory, or TTS."""

from __future__ import annotations

import json
import re
from typing import Any

_INTERNAL_LINE_PATTERNS = (
    re.compile(r"^\s*(?:to=functions\.|recipient_name\s*:|await\s+tools\.|tools\.)"),
    re.compile(r"^\s*(?:tool_calls?|function_call|arguments)\s*[:=]", re.IGNORECASE),
    re.compile(r"^\s*(?:mcp__|run_officejs|shell_command|exec\()", re.IGNORECASE),
    re.compile(r"^\s*(?:analysis|reasoning|思考)\s*:", re.IGNORECASE),
)
_FENCE_PATTERN = re.compile(chr(96) * 3 + r"([\w+-]*)\n([\s\S]*?)" + chr(96) * 3)
_ALWAYS_INTERNAL_JSON_KEYS = {
    "tool_call",
    "tool_calls",
    "recipient_name",
    "function_call",
    "tool_uses",
}
_INTERNAL_JSON_FRAGMENT_PATTERN = re.compile(
    r'"(?:recipient_name|tool_calls?|function_call|tool_uses)"\s*:', re.IGNORECASE
)
_FALLBACK_TEXT = "我刚才差点把内部操作当成台词说出来了，我们重新说。"
_SPEECH_BOUNDARIES = frozenset("。！？!?；;\n")


class AssistantOutputSafetyError(RuntimeError):
    """Raised when an incremental reply would contradict already released speech."""


def sanitize_assistant_text(value: str) -> str:
    """Remove accidental tool-call or reasoning leakage from model-facing replies."""

    text = value.strip()
    if not text:
        return text
    if _looks_like_internal_json(text):
        return _FALLBACK_TEXT
    text = _extract_final_answer(text)
    text = _strip_internal_fences(text)
    lines = [
        line
        for line in text.splitlines()
        if not _is_internal_line(line)
    ]
    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return _FALLBACK_TEXT
    if _looks_like_internal_json(cleaned):
        return _FALLBACK_TEXT
    return cleaned


class IncrementalAssistantSpeech:
    """Release only stable, sanitized sentence prefixes from an LLM stream."""

    def __init__(self) -> None:
        self._raw = ""
        self._released = ""

    @property
    def raw_text(self) -> str:
        return self._raw

    def push(self, chunk: str) -> str:
        if chunk:
            self._raw += chunk
        return self._release(final=False)

    def finish(self) -> tuple[str, str]:
        emitted = self._release(final=True)
        return emitted, sanitize_assistant_text(self._raw)

    def _release(self, *, final: bool) -> str:
        if not final and (
            _requires_more_context(self._raw)
            or (_has_reasoning_marker(self._raw) and not self._released)
        ):
            return ""
        sanitized = sanitize_assistant_text(self._raw)
        if not sanitized.startswith(self._released):
            raise AssistantOutputSafetyError(
                "sanitized assistant output changed after speech was released"
            )
        boundary = len(sanitized) if final else _last_speech_boundary(sanitized)
        if boundary <= len(self._released):
            return ""
        emitted = sanitized[len(self._released) : boundary]
        self._released = sanitized[:boundary]
        return emitted


def _last_speech_boundary(text: str) -> int:
    return max(
        (index + 1 for index, char in enumerate(text) if char in _SPEECH_BOUNDARIES),
        default=0,
    )


def _requires_more_context(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return True
    if text.count(chr(96) * 3) % 2:
        return True
    lines = text.splitlines()
    return any(line.lstrip().startswith(("{", "[")) for line in lines)


def _has_reasoning_marker(text: str) -> bool:
    return bool(re.search(r"(?:^|\n)\s*(?:analysis|reasoning|思考)\s*:", text, re.I))


def _is_internal_line(line: str) -> bool:
    stripped = line.strip()
    return any(pattern.search(line) for pattern in _INTERNAL_LINE_PATTERNS) or (
        stripped.startswith(("{", "["))
        and (
            _looks_like_internal_json(stripped)
            or bool(_INTERNAL_JSON_FRAGMENT_PATTERN.search(stripped))
        )
    )


def _extract_final_answer(text: str) -> str:
    match = re.search(
        r"(?:^|\n)\s*(?:final|最终回复|最终回答)\s*:\s*(.+)\s*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return text


def _strip_internal_fences(text: str) -> str:
    def replace_fence(match: re.Match[str]) -> str:
        body = match.group(2).strip()
        has_internal_line = any(
            pattern.search(line)
            for line in body.splitlines()
            for pattern in _INTERNAL_LINE_PATTERNS
        )
        if _looks_like_internal_json(body) or has_internal_line:
            return ""
        return match.group(0)

    return _FENCE_PATTERN.sub(replace_fence, text)


def _looks_like_internal_json(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    return _contains_internal_key(parsed)


def _contains_internal_key(value: Any) -> bool:
    if isinstance(value, dict):
        keys = {str(key) for key in value}
        if keys & _ALWAYS_INTERNAL_JSON_KEYS:
            return True
        if "function" in keys and (
            "arguments" in keys or isinstance(value.get("function"), dict)
        ):
            return True
        if keys & {"tool", "tool_name"} and keys & {"arguments", "parameters", "input"}:
            return True
        return any(_contains_internal_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_internal_key(item) for item in value)
    return False

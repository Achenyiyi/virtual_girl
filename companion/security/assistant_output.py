"""Sanitize assistant-visible text before it reaches UI, memory, or TTS."""

from __future__ import annotations

import json
import re
from typing import Any

_INTERNAL_LINE_PATTERNS = (
    re.compile(r"^\s*(?:to=functions\.|recipient_name\s*:|await\s+tools\.|tools\.)"),
    re.compile(r"^\s*(?:tool_calls?|function_call|arguments)\s*[:=]", re.IGNORECASE),
    re.compile(r"^\s*(?:mcp__|run_officejs|shell_command|exec\()", re.IGNORECASE),
)
_FENCE_PATTERN = re.compile(chr(96) * 3 + r"([\w+-]*)\n([\s\S]*?)" + chr(96) * 3)
_ALWAYS_INTERNAL_JSON_KEYS = {
    "tool_call",
    "tool_calls",
    "recipient_name",
    "function_call",
    "tool_uses",
}
_FALLBACK_TEXT = "我刚才差点把内部操作当成台词说出来了，我们重新说。"


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
        if not any(pattern.search(line) for pattern in _INTERNAL_LINE_PATTERNS)
    ]
    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return _FALLBACK_TEXT
    if _looks_like_internal_json(cleaned):
        return _FALLBACK_TEXT
    return cleaned


def _extract_final_answer(text: str) -> str:
    match = re.search(
        r"(?:^|\n)\s*(?:final|最终回复|最终回答)\s*:\s*(.+)\s*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match and re.search(r"(?:^|\n)\s*(?:analysis|reasoning|思考)\s*:", text, re.I):
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

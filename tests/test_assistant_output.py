import pytest

from companion.security.assistant_output import (
    AssistantOutputSafetyError,
    IncrementalAssistantSpeech,
    sanitize_assistant_text,
)


def test_sanitize_assistant_text_keeps_normal_reply() -> None:
    assert sanitize_assistant_text("今天可以慢慢来，我在。") == "今天可以慢慢来，我在。"


def test_sanitize_assistant_text_removes_tool_call_json() -> None:
    cleaned = sanitize_assistant_text(
        '{"recipient_name":"functions.exec","arguments":{"command":"whoami"}}'
    )

    assert "recipient_name" not in cleaned
    assert "functions.exec" not in cleaned


def test_sanitize_assistant_text_keeps_non_tool_json() -> None:
    value = '{"arguments":"这是对术语的普通说明","result":"可见"}'

    assert sanitize_assistant_text(value) == value


def test_sanitize_assistant_text_extracts_final_after_reasoning() -> None:
    assert sanitize_assistant_text("analysis: hidden\nfinal: 好呀，我听着。") == "好呀，我听着。"


def test_sanitize_assistant_text_removes_standalone_reasoning() -> None:
    cleaned = sanitize_assistant_text("analysis: 不应显示。")

    assert "不应显示" not in cleaned


def test_sanitize_assistant_text_unwraps_standalone_final() -> None:
    assert sanitize_assistant_text("final: 好呀，我听着。") == "好呀，我听着。"


def test_sanitize_assistant_text_removes_internal_tool_lines() -> None:
    cleaned = sanitize_assistant_text("我先看一下。\nawait tools.shell_command({})\n可以继续。")

    assert cleaned == "我先看一下。\n可以继续。"


def test_sanitize_assistant_text_removes_embedded_tool_json_line() -> None:
    cleaned = sanitize_assistant_text(
        '我先看一下。\n{"recipient_name":"functions.exec","arguments":{"command":"whoami"}}'
    )

    assert cleaned == "我先看一下。"


def test_incremental_speech_releases_only_complete_safe_sentences() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push("今天可以") == ""
    assert speech.push("慢慢来。下一句") == "今天可以慢慢来。"
    tail, full = speech.finish()

    assert tail == "下一句"
    assert full == "今天可以慢慢来。下一句"


def test_incremental_speech_holds_reasoning_until_final_answer_is_known() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push("analysis: 不应朗读。\n") == ""
    assert speech.push("final: 好呀，我听着。") == ""
    tail, full = speech.finish()

    assert tail == "好呀，我听着。"
    assert full == "好呀，我听着。"


def test_incremental_speech_never_releases_partial_tool_json() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push('{"recipient_name":"functions.exec",') == ""
    assert speech.push('"arguments":{"command":"whoami"}}') == ""
    tail, full = speech.finish()

    assert tail == full
    assert "recipient_name" not in tail


def test_incremental_speech_drops_truncated_internal_json_on_finish() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push('{"recipient_name":"functions.exec",') == ""
    tail, full = speech.finish()

    assert tail == full
    assert "recipient_name" not in tail


def test_incremental_speech_holds_embedded_tool_json_without_emitting_a_newline() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push("好的。") == "好的。"
    assert speech.push('\n{"recipient_name":"functions.exec",') == ""
    assert speech.push('"arguments":{"command":"whoami"}}') == ""
    tail, full = speech.finish()

    assert tail == ""
    assert full == "好的。"


def test_incremental_speech_fails_closed_when_sanitized_prefix_changes() -> None:
    speech = IncrementalAssistantSpeech()

    assert speech.push("先说一句。") == "先说一句。"
    with pytest.raises(AssistantOutputSafetyError):
        speech.push("\nanalysis: 不应朗读。\nfinal: 完全不同。")

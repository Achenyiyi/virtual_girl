from companion.security.assistant_output import sanitize_assistant_text


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


def test_sanitize_assistant_text_removes_internal_tool_lines() -> None:
    cleaned = sanitize_assistant_text("我先看一下。\nawait tools.shell_command({})\n可以继续。")

    assert cleaned == "我先看一下。\n可以继续。"

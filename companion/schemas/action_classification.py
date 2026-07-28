"""Action classification — risk levels, methods, and permission categories.

From the PLAN:
"所有写操作经过权限分类：只读自动执行；可逆低风险操作可按用户策略执行；
发送消息、付款、删除、安装、账号权限等必须逐次确认并显示预览"
"""

from __future__ import annotations

from enum import StrEnum


class RiskLevel(StrEnum):
    """Risk classification for computer actions."""

    READONLY = "readonly"
    REVERSIBLE_LOW = "reversible_low"
    REVERSIBLE_HIGH = "reversible_high"
    IRREVERSIBLE = "irreversible"


class ActionMethod(StrEnum):
    """Execution method priority (higher = preferred)."""

    API = "api"  # Official API/MCP
    DOM = "dom"  # Browser DOM / accessibility tree
    UIA = "uia"  # Windows UI Automation
    VISION = "vision_fallback"  # Screenshot + vision model (last resort)


class ActionCategory(StrEnum):
    """Categories of actions the companion can perform."""

    SYSTEM = "system"  # Open app, focus window, check status
    WEB = "web"  # Search, browse, read pages
    FILE = "file"  # Create, read, edit, delete files
    COMMUNICATION = "communication"  # Send messages, emails
    MEDIA = "media"  # Play, pause, volume control
    CALENDAR = "calendar"  # Check, create events
    CUSTOM = "custom"  # User-defined actions


# Action classification table
# Maps action_type → (risk_level, default_method, requires_confirmation, can_undo)
ACTION_CLASSIFICATIONS: dict[str, tuple[RiskLevel, ActionMethod, bool, bool]] = {
    # Read-only — auto execute
    "read_window_title": (RiskLevel.READONLY, ActionMethod.UIA, False, False),
    "read_active_app": (RiskLevel.READONLY, ActionMethod.UIA, False, False),
    "read_clipboard": (RiskLevel.READONLY, ActionMethod.API, False, False),
    "check_system_status": (RiskLevel.READONLY, ActionMethod.API, False, False),
    # Reversible, low risk — auto per user policy
    "open_app": (RiskLevel.REVERSIBLE_LOW, ActionMethod.API, False, False),
    "focus_window": (RiskLevel.REVERSIBLE_LOW, ActionMethod.UIA, False, False),
    "search_web": (RiskLevel.REVERSIBLE_LOW, ActionMethod.DOM, False, False),
    "play_media": (RiskLevel.REVERSIBLE_LOW, ActionMethod.API, False, True),
    "pause_media": (RiskLevel.REVERSIBLE_LOW, ActionMethod.API, False, True),
    "volume_up": (RiskLevel.REVERSIBLE_LOW, ActionMethod.API, False, True),
    "volume_down": (RiskLevel.REVERSIBLE_LOW, ActionMethod.API, False, True),
    # Reversible, high risk — requires confirmation with preview
    "create_file": (RiskLevel.REVERSIBLE_HIGH, ActionMethod.API, True, True),
    "edit_file": (RiskLevel.REVERSIBLE_HIGH, ActionMethod.API, True, True),
    "create_calendar_event": (RiskLevel.REVERSIBLE_HIGH, ActionMethod.API, True, True),
    # Irreversible — requires explicit confirmation, preview, cooldown
    "send_message": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "send_email": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "delete_file": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "install_software": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "modify_settings": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "payment": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
    "account_action": (RiskLevel.IRREVERSIBLE, ActionMethod.API, True, False),
}


def get_action_classification(action_type: str) -> tuple[RiskLevel, ActionMethod, bool, bool]:
    """Look up the classification for an action type.

    Unknown action types default to IRREVERSIBLE with confirmation required
    (fail-safe).
    """
    return ACTION_CLASSIFICATIONS.get(
        action_type,
        (RiskLevel.IRREVERSIBLE, ActionMethod.VISION, True, False),
    )

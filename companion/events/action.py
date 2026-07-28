"""Action events — computer operation lifecycle.

All computer actions go through a strict permission pipeline:
1. API/DOM/UIA first (programmatic, reliable)
2. Screenshot + visual only as fallback
3. Write/delete/send actions require explicit confirmation

Every action is audited with before/after state, success/failure,
and permission trace.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from companion.events.base import BaseEvent


class ActionRequestedEvent(BaseEvent):
    """A computer action was requested (by companion or policy)."""

    __event_type__: ClassVar[str] = "action.requested"

    action_id: str = Field(..., description="Unique action identifier")
    action_type: str = Field(
        ...,
        description="e.g., 'open_app', 'search_web', 'create_file', 'send_message'",
    )
    method: str = Field(
        ...,
        description="Execution method: 'api', 'dom', 'uia', 'vision_fallback'",
    )
    parameters: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    risk_level: str = Field(
        ...,
        description="'readonly', 'reversible_low', 'reversible_high', 'irreversible'",
    )
    requires_confirmation: bool = Field(default=False)
    requested_by: str = Field(..., description="Which component requested this action")


class ActionConfirmedEvent(BaseEvent):
    """User confirmed (or denied) a pending action."""

    __event_type__: ClassVar[str] = "action.confirmed"

    action_id: str = Field(..., description="Action being confirmed")
    approved: bool = Field(...)
    user_response_time_ms: int = Field(default=0, ge=0)
    override_method: str | None = Field(
        default=None,
        description="If user chose a different execution method",
    )


class ActionExecutedEvent(BaseEvent):
    """An action was executed with its outcome."""

    __event_type__: ClassVar[str] = "action.executed"

    action_id: str = Field(...)
    success: bool = Field(...)
    duration_ms: int = Field(default=0, ge=0)
    method_used: str = Field(..., description="Actual method used for execution")
    error_message: str | None = Field(default=None)
    retry_count: int = Field(default=0, ge=0)
    state_before: dict[str, Any] | None = Field(default=None, description="Snapshot before action")
    state_after: dict[str, Any] | None = Field(default=None, description="Snapshot after action")


class ActionRevokedEvent(BaseEvent):
    """A previously executed action was undone/rolled back."""

    __event_type__: ClassVar[str] = "action.revoked"

    original_action_id: str = Field(...)
    revoke_action_id: str = Field(..., description="The undo action's ID")
    success: bool = Field(...)
    remaining_effects: list[str] = Field(
        default_factory=list,
        description="Side effects that could not be reversed",
    )


class ToolAuditEvent(BaseEvent):
    """Audit log entry for any tool invocation (for security monitoring)."""

    __event_type__: ClassVar[str] = "action.tool_audit"

    tool_name: str = Field(...)
    tool_parameters_summary: str = Field(
        default="",
        description="Redacted summary of parameters (no secrets)",
    )
    risk_level: str = Field(...)
    permission_checked: bool = Field(default=True)
    sandbox_id: str | None = Field(default=None, description="Isolation sandbox identifier")
    caller_component: str = Field(...)
    ip_address: str | None = Field(default=None)
    user_present: bool = Field(default=True)

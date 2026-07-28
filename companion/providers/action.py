"""Action provider interface — computer operation capabilities.

Actions follow a strict permission hierarchy:
1. API/MCP calls (preferred — deterministic, auditable)
2. Browser DOM / Accessibility Tree (structured, reliable)
3. Windows UI Automation (programmatic control)
4. Screenshot + Vision + Mouse/Keyboard (last resort)

Risk classification:
- readonly: automatic execution allowed
- reversible_low: can auto-execute per user policy
- reversible_high: requires confirmation with preview
- irreversible: requires explicit confirmation, preview, and cooldown
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from companion.providers.base import Provider, ProviderHealth, ProviderInfo


@dataclass
class ActionRequest:
    """A request to perform a computer action."""

    action_id: str
    action_type: str  # 'open_app', 'search_web', 'create_file', 'send_message', etc.
    method: str  # 'api', 'dom', 'uia', 'vision_fallback'
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "readonly"  # readonly, reversible_low, reversible_high, irreversible
    requires_confirmation: bool = False
    timeout_ms: int = 30000
    sandbox_id: str | None = None


@dataclass
class ActionResult:
    """Result of executing a computer action."""

    action_id: str
    success: bool
    method_used: str
    duration_ms: int = 0
    result_data: dict[str, Any] | None = None
    error_message: str | None = None
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    can_undo: bool = False
    undo_action_id: str | None = None


@dataclass(frozen=True)
class SandboxStatus:
    """Provider evidence that an action will execute inside an isolated boundary."""

    verified: bool
    sandbox_id: str = ""
    isolation_level: str = "none"
    reason: str = ""


@dataclass
class ActionPermission:
    """A permission rule for a class of actions."""

    action_pattern: str  # e.g., 'open_app:*', 'search_web:*', 'send_message:*'
    risk_level: str
    auto_approve: bool = False
    require_confirmation: bool = True
    require_user_present: bool = True
    cooldown_seconds: int = 0
    max_per_hour: int = 10
    allowed_apps: list[str] = field(default_factory=list)
    blocked_apps: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)


class ActionProvider(Provider):
    """Abstract interface for computer action execution.

    Implementations:
    - WindowsUIAProvider: Windows UI Automation
    - BrowserUseProvider: browser-use for web actions
    - UFOProvider: Microsoft UFO for GUI agent fallback
    - NullActionProvider: no-op for safe mode / testing
    """

    @abstractmethod
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Execute a computer action and return the result."""
        ...

    @abstractmethod
    async def undo(self, action_id: str) -> ActionResult:
        """Attempt to undo a previously executed action."""
        ...

    @abstractmethod
    async def preview(self, request: ActionRequest) -> dict[str, Any]:
        """Preview what an action will do without executing it.

        Returns a description of the expected effect.
        """
        ...

    @abstractmethod
    async def verify_sandbox(self, request: ActionRequest) -> SandboxStatus:
        """Return current sandbox evidence; execution is denied unless verified."""
        ...

    @abstractmethod
    async def get_permissions(self) -> list[ActionPermission]:
        """Get the current permission rules."""
        ...

    @abstractmethod
    async def update_permissions(self, permissions: list[ActionPermission]) -> None:
        """Update permission rules."""
        ...

    @abstractmethod
    async def get_audit_log(
        self, limit: int = 100, risk_level: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve the action audit log."""
        ...

    @abstractmethod
    def provider_info(self) -> ProviderInfo: ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

"""Action Service — managed computer operations with sandboxing and permission gates.

Phase 4 core component. Implements the four-tier action execution hierarchy:
1. API/MCP calls (preferred)
2. Browser DOM / Accessibility Tree
3. Windows UI Automation
4. Screenshot + Vision (last resort)

Key design from the PLAN:
"所有写操作经过权限分类：只读自动执行；可逆低风险操作可按用户策略执行；
发送消息、付款、删除、安装、账号权限等必须逐次确认并显示预览"
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from companion.async_util import wait_with_timeout
from companion.core.event_bus import EventBus
from companion.core.policy_gate import PolicyGate
from companion.events.base import generate_ulid
from companion.providers.action import (
    ActionProvider,
    ActionRequest,
    ActionResult,
)
from companion.schemas.action_classification import (
    RiskLevel,
    get_action_classification,
)
from companion.security.action_audit import ActionAuditEntry, ActionAuditStore
from companion.security.redaction import redact_mapping, redact_text

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ActionState(StrEnum):
    PENDING = "pending"
    WAITING_CONFIRMATION = "waiting_confirmation"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REVOKED = "revoked"
    DENIED = "denied"


class ActionProviderTimeoutError(TimeoutError):
    def __init__(self, operation_name: str) -> None:
        super().__init__(operation_name)
        self.operation_name = operation_name


@dataclass
class ActionRecord:
    """Internal tracking record for an action."""

    request: ActionRequest
    state: ActionState = ActionState.PENDING
    result: ActionResult | None = None
    created_at: float = 0.0
    confirmed_at: float | None = None
    executed_at: float | None = None
    user_approved: bool | None = None
    preview: dict[str, Any] | None = None


@dataclass
class ActionServiceConfig:
    """Configuration for the action service."""

    sandbox_enabled: bool = True
    max_concurrent_actions: int = 1
    action_timeout_seconds: float = 30.0
    undo_enabled: bool = True
    audit_enabled: bool = True
    require_durable_audit: bool = True
    allow_readonly_auto: bool = True
    allow_reversible_low_auto: bool = True
    allow_reversible_high_auto: bool = False
    allow_irreversible_auto: bool = False
    max_history_entries: int = 1000
    max_in_memory_audit_entries: int = 1000
    max_pending_confirmations: int = 10
    confirmation_ttl_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_concurrent_actions < 1:
            raise ValueError("max_concurrent_actions must be at least 1")
        if self.action_timeout_seconds <= 0:
            raise ValueError("action_timeout_seconds must be positive")
        if self.require_durable_audit and not self.audit_enabled:
            raise ValueError("durable action audit requires audit_enabled")
        if self.max_history_entries < 1 or self.max_in_memory_audit_entries < 1:
            raise ValueError("action history and in-memory audit limits must be positive")
        if self.max_pending_confirmations < 1 or self.confirmation_ttl_seconds <= 0:
            raise ValueError("pending confirmation limits and TTL must be positive")


class ActionService:
    """Manages computer actions with permission gates and sandboxing."""

    def __init__(
        self,
        provider: ActionProvider | None = None,
        bus: EventBus | None = None,
        config: ActionServiceConfig | None = None,
        policy_gate: PolicyGate | None = None,
        audit_store: ActionAuditStore | None = None,
    ) -> None:
        self._provider = provider
        self._bus = bus
        self._config = config or ActionServiceConfig()
        self._policy_gate = policy_gate or PolicyGate()
        self._audit_store = audit_store

        self._history: deque[ActionRecord] = deque(maxlen=self._config.max_history_entries)
        self._pending: dict[str, ActionRecord] = {}
        self._active_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            self._config.max_concurrent_actions
        )
        self._confirmation_lock = asyncio.Lock()
        self._audit_log: deque[dict[str, Any]] = deque(
            maxlen=self._config.max_in_memory_audit_entries
        )
        self._provider_quarantined_reason = ""
        self._audit_store_quarantined = False

    def _record_history(self, record: ActionRecord) -> None:
        self._history.append(record)

    async def _expire_stale_confirmations(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - self._config.confirmation_ttl_seconds
        expired = [
            record
            for record in self._pending.values()
            if record.state == ActionState.WAITING_CONFIRMATION and record.created_at < cutoff
        ]
        for record in expired:
            self._pending.pop(record.request.action_id, None)
            record.state = ActionState.REVOKED
            record.result = ActionResult(
                action_id=record.request.action_id,
                success=False,
                method_used=record.request.method,
                error_message="Action confirmation expired",
            )
            self._record_history(record)
            await self._audit(
                record,
                "confirmation_expired",
                success=False,
            )

    # ── Action execution ──────────────────────────────────────────────

    async def request(
        self,
        action_type: str,
        parameters: dict[str, Any] | None = None,
        method: str = "api",
    ) -> tuple[ActionRecord, ActionResult | None]:
        """Request a computer action. May block if confirmation is needed.

        Returns:
            (record, result) — if result is None, action is pending confirmation
        """
        parameters = parameters or {}

        # PolicyGate is the authoritative authorization path. Configuration may
        # make lower-risk operations stricter, but can never relax the gate.
        risk_level, default_method, requires_conf, _ = get_action_classification(action_type)
        method = method if method != "api" else default_method
        decision = self._policy_gate.evaluate_action(action_type, parameters)
        if not decision.approved:
            request = ActionRequest(
                action_id=f"act_{generate_ulid()}",
                action_type=action_type,
                method=method,
                parameters=parameters,
                risk_level=risk_level,
                requires_confirmation=False,
                timeout_ms=int(self._config.action_timeout_seconds * 1000),
            )
            record = ActionRecord(
                request=request,
                state=ActionState.DENIED,
                created_at=time.time(),
            )
            self._record_history(record)
            await self._audit(
                record,
                "denied",
                success=False,
                details={"reason": decision.reason, "parameters": parameters},
            )
            return record, None

        auto_allow = self._check_auto_allow(risk_level)
        needs_confirmation = decision.requires_user_confirmation or requires_conf or not auto_allow
        request = ActionRequest(
            action_id=f"act_{generate_ulid()}",
            action_type=action_type,
            method=method,
            parameters=parameters,
            risk_level=risk_level,
            requires_confirmation=needs_confirmation,
            timeout_ms=int(self._config.action_timeout_seconds * 1000),
        )

        record = ActionRecord(
            request=request,
            state=ActionState.WAITING_CONFIRMATION if needs_confirmation else ActionState.PENDING,
            created_at=time.time(),
        )
        if self._provider_quarantined_reason:
            result = ActionResult(
                action_id=request.action_id,
                success=False,
                method_used=method,
                error_message=(
                    "Action provider is quarantined after a timed-out operation; restart required"
                ),
            )
            record.state = ActionState.FAILED
            record.result = result
            self._record_history(record)
            await self._audit(
                record,
                "provider_quarantined",
                success=False,
                details={"reason": self._provider_quarantined_reason},
            )
            return record, result
        if needs_confirmation:
            async with self._confirmation_lock:
                await self._expire_stale_confirmations()
                pending_count = sum(
                    item.state == ActionState.WAITING_CONFIRMATION
                    for item in self._pending.values()
                )
                if pending_count >= self._config.max_pending_confirmations:
                    result = ActionResult(
                        action_id=request.action_id,
                        success=False,
                        method_used=method,
                        error_message="Too many actions are awaiting confirmation",
                    )
                    record.state = ActionState.DENIED
                    record.result = result
                    self._record_history(record)
                    await self._audit(
                        record,
                        "confirmation_capacity_exceeded",
                        success=False,
                    )
                    return record, result
                self._pending[request.action_id] = record
        else:
            self._pending[request.action_id] = record

        if not await self._audit(
            record,
            "requested",
            details={"method": method, "parameters": parameters},
        ):
            result = ActionResult(
                action_id=request.action_id,
                success=False,
                method_used=method,
                error_message="Durable action audit is unavailable",
            )
            record.state = ActionState.FAILED
            record.result = result
            self._pending.pop(request.action_id, None)
            self._record_history(record)
            return record, result

        if needs_confirmation and self._provider:
            try:
                record.preview = await self._await_provider_operation(
                    self._provider.preview(request),
                    operation_name="preview",
                )
            except Exception as exc:
                record.state = ActionState.FAILED
                self._pending.pop(request.action_id, None)
                result = ActionResult(
                    action_id=request.action_id,
                    success=False,
                    method_used=method,
                    error_message=f"Action preview failed: {type(exc).__name__}",
                )
                record.result = result
                self._record_history(record)
                await self._audit(
                    record,
                    "preview_failed",
                    success=False,
                    details={"error": result.error_message},
                )
                return record, result

        # Publish event
        if self._bus:
            from companion.events.action import ActionRequestedEvent

            await self._bus.publish(
                ActionRequestedEvent(
                    action_id=request.action_id,
                    action_type=action_type,
                    method=method,
                    parameters=self._redact_parameters(parameters),
                    risk_level=risk_level,
                    requires_confirmation=needs_confirmation,
                    requested_by="action_service",
                )
            )

        if needs_confirmation:
            return record, None

        # Execute immediately
        result = await self._execute(record)
        return record, result

    async def confirm(self, action_id: str, approved: bool) -> ActionResult | None:
        """Confirm or deny a pending action."""
        async with self._confirmation_lock:
            await self._expire_stale_confirmations()
            record = self._pending.get(action_id)
            if not record:
                logger.warning("Action %s not found for confirmation", action_id)
                return None
            if record.state != ActionState.WAITING_CONFIRMATION:
                logger.warning(
                    "Action %s is not awaiting confirmation (state=%s)",
                    action_id,
                    record.state,
                )
                return record.result

            record.user_approved = approved
            record.confirmed_at = time.time()

            if not approved:
                record.state = ActionState.DENIED
                self._pending.pop(action_id, None)
                self._record_history(record)
            else:
                # Claim the confirmation before releasing the lock so a concurrent
                # duplicate cannot execute the same action.
                record.state = ActionState.PENDING
                record.request.requires_confirmation = False

        if self._bus:
            from companion.events.action import ActionConfirmedEvent

            await self._bus.publish(
                ActionConfirmedEvent(
                    action_id=action_id,
                    approved=approved,
                )
            )

        audit_ok = await self._audit(
            record,
            "confirmed",
            success=approved,
            details={"approved": approved},
        )
        if approved and not audit_ok:
            record.state = ActionState.FAILED
            result = ActionResult(
                action_id=action_id,
                success=False,
                method_used=record.request.method,
                error_message="Durable action audit is unavailable",
            )
            record.result = result
            self._pending.pop(action_id, None)
            self._record_history(record)
            return result

        if not approved:
            return None
        return await self._execute(record)

    async def _execute(self, record: ActionRecord) -> ActionResult:
        execution_task = asyncio.create_task(self._execute_committed(record))
        try:
            return await asyncio.shield(execution_task)
        except asyncio.CancelledError:
            await execution_task
            raise

    async def _execute_committed(self, record: ActionRecord) -> ActionResult:
        """Execute a confirmed action."""
        # Wait for concurrency slot (Semaphore-based, non-polling)
        async with self._active_semaphore:
            record.state = ActionState.EXECUTING
            record.executed_at = time.time()

            try:
                if self._provider_quarantined_reason:
                    result = self._quarantined_result(record.request)
                elif self._provider:
                    if self._config.sandbox_enabled:
                        sandbox = await self._await_provider_operation(
                            self._provider.verify_sandbox(record.request),
                            operation_name="sandbox verification",
                        )
                        if not sandbox.verified or not sandbox.sandbox_id:
                            result = ActionResult(
                                action_id=record.request.action_id,
                                success=False,
                                method_used=record.request.method,
                                error_message="Sandbox verification failed",
                            )
                        else:
                            record.request.sandbox_id = sandbox.sandbox_id
                            result = await self._execute_provider(record, sandbox.isolation_level)
                    else:
                        result = await self._execute_provider(record, "disabled")
                else:
                    result = ActionResult(
                        action_id=record.request.action_id,
                        success=False,
                        method_used="none",
                        error_message="No action provider configured",
                    )
            except ActionProviderTimeoutError as exc:
                outcome = (
                    "; provider outcome is unknown"
                    if exc.operation_name in {"execution", "undo execution"}
                    else ""
                )
                result = ActionResult(
                    action_id=record.request.action_id,
                    success=False,
                    method_used=record.request.method,
                    error_message=(
                        f"Action {exc.operation_name} timed out after "
                        f"{self._config.action_timeout_seconds:.1f}s{outcome}"
                    ),
                )
            except Exception as e:
                result = ActionResult(
                    action_id=record.request.action_id,
                    success=False,
                    method_used=record.request.method,
                    error_message=redact_text(e),
                )

        record.state = ActionState.COMPLETED if result.success else ActionState.FAILED
        record.result = result
        self._record_history(record)
        self._pending.pop(record.request.action_id, None)

        # Publish execution event
        if self._bus:
            from companion.events.action import ActionExecutedEvent

            await self._bus.publish(
                ActionExecutedEvent(
                    action_id=record.request.action_id,
                    success=result.success,
                    duration_ms=int((time.time() - (record.executed_at or time.time())) * 1000),
                    method_used=result.method_used,
                    error_message=result.error_message,
                )
            )

        audit_ok = await self._audit(
            record,
            "executed",
            success=result.success,
            details={
                "method": result.method_used,
                "duration_ms": result.duration_ms,
                "error": result.error_message,
                "sandbox_id": record.request.sandbox_id,
            },
        )
        if not audit_ok:
            logger.critical(
                "Action %s completed but its final audit record was not persisted",
                record.request.action_id,
            )

        return result

    async def _execute_provider(self, record: ActionRecord, isolation_level: str) -> ActionResult:
        """Persist execution intent before crossing the provider side-effect boundary."""
        if not self._provider:
            raise RuntimeError("No action provider configured")
        audit_ok = await self._audit(
            record,
            "executing",
            details={
                "method": record.request.method,
                "sandbox_id": record.request.sandbox_id,
                "isolation_level": isolation_level,
            },
        )
        if not audit_ok:
            return ActionResult(
                action_id=record.request.action_id,
                success=False,
                method_used=record.request.method,
                error_message="Durable action audit is unavailable",
            )
        return await self._await_provider_operation(
            self._provider.execute(record.request),
            operation_name="execution",
        )

    async def undo(self, action_id: str) -> ActionResult | None:
        """Undo an eligible action through the same audit, sandbox, and timeout boundary."""
        if not self._provider or not self._config.undo_enabled:
            return None
        if self._provider_quarantined_reason:
            return self._quarantined_result(
                ActionRequest(
                    action_id=f"revoke_{generate_ulid()}",
                    action_type="undo",
                    method="none",
                )
            )

        original = next(
            (record for record in reversed(self._history) if record.request.action_id == action_id),
            None,
        )
        if not original or not original.result or not original.result.success:
            return None
        classification_can_undo = get_action_classification(original.request.action_type)[3]
        if not classification_can_undo and not original.result.can_undo:
            return None

        undo_request = ActionRequest(
            action_id=f"revoke_{generate_ulid()}",
            action_type=f"undo:{original.request.action_type}",
            method=original.request.method,
            parameters={"original_action_id": action_id},
            risk_level=RiskLevel.REVERSIBLE_HIGH,
            timeout_ms=int(self._config.action_timeout_seconds * 1000),
        )
        undo_record = ActionRecord(
            request=undo_request,
            state=ActionState.PENDING,
            created_at=time.time(),
        )
        if not await self._audit(
            undo_record,
            "undo_requested",
            details={"original_action_id": action_id},
        ):
            return ActionResult(
                action_id=undo_request.action_id,
                success=False,
                method_used=undo_request.method,
                error_message="Durable action audit is unavailable",
            )

        undo_task = asyncio.create_task(
            self._undo_committed(undo_record, original_action_id=action_id)
        )
        try:
            return await asyncio.shield(undo_task)
        except asyncio.CancelledError:
            await undo_task
            raise

    async def _undo_committed(
        self,
        undo_record: ActionRecord,
        *,
        original_action_id: str,
    ) -> ActionResult:
        undo_request = undo_record.request
        provider = self._provider
        if provider is None:
            raise RuntimeError("No action provider configured")
        async with self._active_semaphore:
            undo_record.state = ActionState.EXECUTING
            undo_record.executed_at = time.time()
            try:
                if self._config.sandbox_enabled:
                    sandbox = await self._await_provider_operation(
                        provider.verify_sandbox(undo_request),
                        operation_name="undo sandbox verification",
                    )
                    if not sandbox.verified or not sandbox.sandbox_id:
                        result = ActionResult(
                            action_id=undo_request.action_id,
                            success=False,
                            method_used=undo_request.method,
                            error_message="Sandbox verification failed",
                        )
                    else:
                        undo_request.sandbox_id = sandbox.sandbox_id
                        result = await self._undo_provider(undo_record, original_action_id)
                else:
                    result = await self._undo_provider(undo_record, original_action_id)
            except ActionProviderTimeoutError as exc:
                outcome = (
                    "; provider outcome is unknown"
                    if exc.operation_name == "undo execution"
                    else ""
                )
                result = ActionResult(
                    action_id=undo_request.action_id,
                    success=False,
                    method_used=undo_request.method,
                    error_message=(
                        f"Action {exc.operation_name} timed out after "
                        f"{self._config.action_timeout_seconds:.1f}s{outcome}"
                    ),
                )
            except Exception as exc:
                result = ActionResult(
                    action_id=undo_request.action_id,
                    success=False,
                    method_used=undo_request.method,
                    error_message=redact_text(exc),
                )

        undo_record.state = ActionState.REVOKED if result.success else ActionState.FAILED
        undo_record.result = result
        self._record_history(undo_record)

        if self._bus:
            from companion.events.action import ActionRevokedEvent

            await self._bus.publish(
                ActionRevokedEvent(
                    original_action_id=original_action_id,
                    revoke_action_id=undo_request.action_id,
                    success=result.success,
                )
            )

        audit_ok = await self._audit(
            undo_record,
            "undo_executed",
            success=result.success,
            details={
                "original_action_id": original_action_id,
                "error": result.error_message,
                "sandbox_id": undo_request.sandbox_id,
            },
        )
        if not audit_ok:
            logger.critical(
                "Undo %s completed but its final audit record was not persisted",
                undo_request.action_id,
            )

        return result

    async def _undo_provider(self, record: ActionRecord, original_action_id: str) -> ActionResult:
        if not self._provider:
            raise RuntimeError("No action provider configured")
        if not await self._audit(
            record,
            "undo_executing",
            details={
                "original_action_id": original_action_id,
                "sandbox_id": record.request.sandbox_id,
            },
        ):
            return ActionResult(
                action_id=record.request.action_id,
                success=False,
                method_used=record.request.method,
                error_message="Durable action audit is unavailable",
            )
        return await self._await_provider_operation(
            self._provider.undo(original_action_id),
            operation_name="undo execution",
        )

    async def _await_provider_operation(
        self,
        operation: Awaitable[T],
        *,
        operation_name: str,
    ) -> T:
        task: asyncio.Future[T] = asyncio.ensure_future(operation)
        try:
            if not await wait_with_timeout(task, self._config.action_timeout_seconds):
                self._provider_quarantined_reason = operation_name
                logger.critical(
                    "Action provider %s exceeded %.1fs and was quarantined",
                    operation_name,
                    self._config.action_timeout_seconds,
                )
                raise ActionProviderTimeoutError(operation_name)
        except asyncio.CancelledError:
            self._provider_quarantined_reason = f"cancelled {operation_name}"
            logger.critical(
                "Action provider %s was cancelled and quarantined because its outcome is unknown",
                operation_name,
            )
            raise
        if task.cancelled():
            self._provider_quarantined_reason = f"self-cancelled {operation_name}"
            raise RuntimeError(f"Action provider {operation_name} cancelled unexpectedly")
        return await task

    @staticmethod
    def _quarantined_result(request: ActionRequest) -> ActionResult:
        return ActionResult(
            action_id=request.action_id,
            success=False,
            method_used=request.method,
            error_message=(
                "Action provider is quarantined after a timed-out operation; restart required"
            ),
        )

    # ── Policy ────────────────────────────────────────────────────────

    def _check_auto_allow(self, risk_level: RiskLevel) -> bool:
        """Check if an action risk level is auto-allowed by policy."""
        auto_map = {
            RiskLevel.READONLY: self._config.allow_readonly_auto,
            RiskLevel.REVERSIBLE_LOW: self._config.allow_reversible_low_auto,
            RiskLevel.REVERSIBLE_HIGH: self._config.allow_reversible_high_auto,
            # Irreversible actions are never auto-approved, regardless of config.
            RiskLevel.IRREVERSIBLE: False,
        }
        return auto_map.get(risk_level, False)

    @classmethod
    def _redact_parameters(cls, parameters: dict[str, Any]) -> dict[str, Any]:
        """Return an audit-safe copy of action parameters."""
        result = redact_mapping(parameters)
        return result if isinstance(result, dict) else {}

    async def _audit(
        self,
        record: ActionRecord,
        stage: str,
        *,
        success: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Write a redacted audit record and report whether durability is satisfied."""
        if not self._config.audit_enabled:
            return not self._config.require_durable_audit
        if self._audit_store_quarantined:
            return False
        safe_details = self._redact_parameters(details or {})
        summary = {
            "action_id": record.request.action_id,
            "action_type": record.request.action_type,
            "risk_level": str(record.request.risk_level),
            "success": success,
            "timestamp": time.time(),
            "stage": stage,
            "details": safe_details,
        }
        self._audit_log.append(summary)
        if self._audit_store is None:
            return not self._config.require_durable_audit
        audit_task = asyncio.ensure_future(
            self._audit_store.append(
                ActionAuditEntry(
                    action_id=record.request.action_id,
                    stage=stage,
                    action_type=record.request.action_type,
                    risk_level=str(record.request.risk_level),
                    success=success,
                    details=safe_details,
                )
            )
        )
        try:
            if not await wait_with_timeout(
                audit_task, self._config.action_timeout_seconds
            ):
                self._audit_store_quarantined = True
                logger.critical(
                    "Action audit persistence timed out for stage %s after %.1fs",
                    stage,
                    self._config.action_timeout_seconds,
                )
                return False
            if audit_task.cancelled():
                self._audit_store_quarantined = True
                logger.critical("Action audit store cancelled stage %s unexpectedly", stage)
                return False
            await audit_task
        except asyncio.CancelledError:
            # Quarantine only when the write is still in flight and its outcome
            # is unknown. A cancellation racing a completed audit write has
            # already persisted the record, so the store must stay usable.
            if not audit_task.done():
                self._audit_store_quarantined = True
                logger.critical(
                    "Action audit store interrupted before stage %s committed; quarantined",
                    stage,
                )
            raise
        except Exception:
            logger.exception("Failed to persist action audit record for stage %s", stage)
            return False
        return True

    # ── Queries ───────────────────────────────────────────────────────

    def get_pending_actions(self) -> list[ActionRecord]:
        """Get actions that need user confirmation."""
        return [r for r in self._pending.values() if r.state == ActionState.WAITING_CONFIRMATION]

    def get_recent_actions(self, limit: int = 50) -> list[ActionRecord]:
        safe_limit = max(0, limit)
        return list(self._history)[-safe_limit:] if safe_limit else []

    def get_action(self, action_id: str) -> ActionRecord | None:
        return self._pending.get(action_id)

    def get_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(0, limit)
        return list(self._audit_log)[-safe_limit:] if safe_limit else []

    def get_stats(self) -> dict[str, Any]:
        total = len(self._history)
        if total == 0:
            return {"total": 0, "success_rate": 1.0}

        succeeded = sum(1 for r in self._history if r.state == ActionState.COMPLETED)
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": sum(1 for r in self._history if r.state == ActionState.FAILED),
            "denied": sum(1 for r in self._history if r.state == ActionState.DENIED),
            "success_rate": succeeded / total,
            "pending": len(self.get_pending_actions()),
        }

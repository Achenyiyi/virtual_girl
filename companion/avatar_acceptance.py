"""Privacy-safe production acceptance gate for a real avatar renderer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from companion import __version__
from companion.providers.avatar import AvatarModel, AvatarState, BodyPose, FacialExpression
from companion.providers.base import ProviderHealth
from companion.providers.implementations.websocket_avatar import (
    AvatarStageInspection,
    WebSocketAvatarProvider,
)


class AvatarAcceptanceProvider(Protocol):
    async def health_check(self) -> ProviderHealth: ...

    async def list_available_models(self) -> list[AvatarModel]: ...

    async def validate_model(self, model_id: str) -> list[str]: ...

    async def load_model(self, model_id: str) -> bool: ...

    async def inspect_stage(self) -> AvatarStageInspection: ...

    async def update_state(self, state: AvatarState) -> None: ...

    async def set_proactive_level(self, level: int) -> None: ...

    async def trigger_expression(
        self, expression_id: str, intensity: float = 0.5, duration_ms: int = 2000
    ) -> None: ...

    async def trigger_gesture(self, gesture_id: str, intensity: float = 0.5) -> None: ...


@dataclass(frozen=True)
class AvatarAcceptanceCheck:
    code: str
    passed: bool
    message: str


@dataclass(frozen=True)
class AvatarAcceptanceReport:
    checks: list[AvatarAcceptanceCheck]

    @property
    def exit_code(self) -> int:
        return 0 if self.checks and all(check.passed for check in self.checks) else 1

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "app_version": __version__,
                "generated_at": datetime.now(UTC).isoformat(),
                "exit_code": self.exit_code,
                "passed": self.exit_code == 0,
                "checks": [asdict(check) for check in self.checks],
            },
            ensure_ascii=False,
            indent=2,
        )


def failed_avatar_acceptance_report(code: str, message: str) -> AvatarAcceptanceReport:
    return AvatarAcceptanceReport([AvatarAcceptanceCheck(code, False, message)])


async def run_avatar_acceptance(
    provider: AvatarAcceptanceProvider,
    *,
    model_id: str,
    apply_timeout_seconds: float = 5.0,
    visual_hold_seconds: float = 0.0,
) -> AvatarAcceptanceReport:
    """Prove model loading and renderer-applied state through the real bridge."""
    if not model_id:
        return failed_avatar_acceptance_report(
            "avatar.model_config", "Configured identity.avatar_model_id is empty."
        )
    if apply_timeout_seconds <= 0:
        raise ValueError("avatar acceptance timeout must be positive")
    if visual_hold_seconds < 0:
        raise ValueError("avatar visual hold must not be negative")

    checks: list[AvatarAcceptanceCheck] = []
    try:
        health = await provider.health_check()
        if health != ProviderHealth.HEALTHY:
            return failed_avatar_acceptance_report(
                "avatar.bridge_health", "Avatar bridge did not report healthy."
            )
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.bridge_health", True, "Authenticated avatar bridge is healthy."
            )
        )

        models = await provider.list_available_models()
        model = next((item for item in models if item.model_id == model_id), None)
        if model is None:
            checks.append(
                AvatarAcceptanceCheck(
                    "avatar.model_available",
                    False,
                    "Configured avatar model was not returned by the stage.",
                )
            )
            return AvatarAcceptanceReport(checks)
        model_type = model.type.lower()
        available = model_type in {"live2d", "vrm"}
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.model_available",
                available,
                "Configured Live2D/VRM model is available."
                if available
                else "Configured model is not a supported Live2D/VRM renderer model.",
            )
        )
        if not available:
            return AvatarAcceptanceReport(checks)

        validation_errors = await provider.validate_model(model_id)
        loaded = not validation_errors and await provider.load_model(model_id)
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.model_loaded",
                loaded,
                "Stage validated and loaded the configured model."
                if loaded
                else "Stage rejected or failed to load the configured model.",
            )
        )
        if not loaded:
            return AvatarAcceptanceReport(checks)

        baseline = await provider.inspect_stage()
        baseline_ok = _baseline_matches(baseline, model_id=model_id, model_type=model_type)
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.renderer_ready",
                baseline_ok,
                "Visible renderer reports the loaded model."
                if baseline_ok
                else "Stage inspection did not prove a visible loaded renderer.",
            )
        )
        if not baseline_ok:
            return AvatarAcceptanceReport(checks)

        expected = AvatarState(
            expression=FacialExpression("happy", intensity=0.73, eye_open=0.91),
            pose=BodyPose(),
            valence=0.61,
            arousal=0.72,
            energy=0.64,
        )
        await provider.update_state(expected)
        await provider.set_proactive_level(3)
        await provider.trigger_expression("happy", intensity=0.73, duration_ms=5000)
        await provider.trigger_gesture("nod", intensity=0.67)
        applied = await _wait_for_rendered_state(
            provider,
            baseline=baseline,
            expected=expected,
            timeout_seconds=apply_timeout_seconds,
        )
        applied_ok = applied is not None
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.state_rendered",
                applied_ok,
                "Renderer applied the state, expression, gesture, and proactive level."
                if applied_ok
                else "Renderer did not expose matching applied state before the deadline.",
            )
        )
        presented = None
        if applied is not None:
            presented = await _wait_for_presented_frame(
                provider,
                applied=applied,
                expected=expected,
                timeout_seconds=apply_timeout_seconds,
            )
        frame_ok = presented is not None
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.frame_presented",
                frame_ok,
                "Renderer frame sequence advanced after the state update."
                if frame_ok
                else "No presented renderer frame was proven after the state update.",
            )
        )
        if frame_ok and visual_hold_seconds:
            await asyncio.sleep(visual_hold_seconds)
        return AvatarAcceptanceReport(checks)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        checks.append(
            AvatarAcceptanceCheck(
                "avatar.runtime",
                False,
                f"Avatar acceptance failed: {type(exc).__name__}.",
            )
        )
        return AvatarAcceptanceReport(checks)


def render_avatar_acceptance_report(report: AvatarAcceptanceReport) -> str:
    lines = ["Avatar acceptance report"]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"[{status}] {check.code}: {check.message}")
    lines.append("Result: PASS" if report.exit_code == 0 else "Result: FAIL")
    return "\n".join(lines)


def _baseline_matches(
    inspection: AvatarStageInspection, *, model_id: str, model_type: str
) -> bool:
    return (
        inspection.renderer.lower() == model_type
        and inspection.model_id == model_id
        and inspection.model_loaded
        and inspection.visible
    )


async def _wait_for_rendered_state(
    provider: AvatarAcceptanceProvider,
    *,
    baseline: AvatarStageInspection,
    expected: AvatarState,
    timeout_seconds: float,
) -> AvatarStageInspection | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        inspection = await provider.inspect_stage()
        if _matches_expected(
            inspection, baseline=baseline, expected=expected, require_advance=True
        ):
            return inspection
        await asyncio.sleep(0.05)
    return None


async def _wait_for_presented_frame(
    provider: AvatarAcceptanceProvider,
    *,
    applied: AvatarStageInspection,
    expected: AvatarState,
    timeout_seconds: float,
) -> AvatarStageInspection | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        inspection = await provider.inspect_stage()
        if (
            inspection.frame_sequence > applied.frame_sequence
            and _matches_expected(
                inspection, baseline=applied, expected=expected, require_advance=False
            )
        ):
            return inspection
        await asyncio.sleep(0.05)
    return None


def _matches_expected(
    inspection: AvatarStageInspection,
    *,
    baseline: AvatarStageInspection,
    expected: AvatarState,
    require_advance: bool,
) -> bool:
    sequences_advanced = (
        inspection.state_sequence > baseline.state_sequence
        and inspection.expression_sequence > baseline.expression_sequence
        and inspection.gesture_sequence > baseline.gesture_sequence
        and inspection.proactive_sequence > baseline.proactive_sequence
    )
    return (
        inspection.visible
        and inspection.state_sequence >= baseline.state_sequence
        and inspection.rendered_state_sequence >= inspection.state_sequence
        and inspection.expression_sequence >= baseline.expression_sequence
        and inspection.gesture_sequence >= baseline.gesture_sequence
        and inspection.proactive_sequence >= baseline.proactive_sequence
        and inspection.expression_id == expected.expression.expression_id
        and inspection.last_gesture_id == "nod"
        and inspection.proactive_level == 3
        and _close(inspection.valence, expected.valence)
        and _close(inspection.arousal, expected.arousal)
        and (not require_advance or sequences_advanced)
    )


def _close(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= 0.01


async def shutdown_avatar_acceptance_provider(
    provider: WebSocketAvatarProvider, *, timeout_seconds: float = 5.0
) -> None:
    """Bound release-gate cleanup even if a third-party WebSocket close stalls."""
    task: asyncio.Future[Any] = asyncio.ensure_future(provider.shutdown())
    done, _ = await asyncio.wait([task], timeout=timeout_seconds)
    if done:
        with contextlib.suppress(Exception):
            await task
        return
    task.cancel()
    task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()

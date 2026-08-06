"""Shared acceptance-gate report model.

Both the avatar and voice release gates produce the same stable JSON
envelope (schema_version, app_version, generated_at, exit_code, passed,
checks) so release tooling can consume either gate identically.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from companion import __version__


@dataclass(frozen=True)
class AcceptanceCheck:
    code: str
    passed: bool
    message: str
    actual_ms: int | None = None
    target_ms: int | None = None


@dataclass(frozen=True)
class AcceptanceReport:
    checks: list[AcceptanceCheck]

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


def failed_report(code: str, message: str) -> AcceptanceReport:
    """Build a stable failure result for setup paths outside the interactive runner."""
    return AcceptanceReport([AcceptanceCheck(code, False, message)])


def render_report(report: AcceptanceReport, *, title: str) -> str:
    lines = [title]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        timing = ""
        if check.actual_ms is not None and check.target_ms is not None:
            timing = f" ({check.actual_ms}ms / target {check.target_ms}ms)"
        lines.append(f"[{status}] {check.code}: {check.message}{timing}")
    lines.append("Result: PASS" if report.exit_code == 0 else "Result: FAIL")
    return "\n".join(lines)

"""Telemetry Service — observability, latency tracking, and health monitoring.

Phase 5 infrastructure. Provides:
- OpenTelemetry trace/span recording for each pipeline stage
- Latency aggregation (p50, p95, p99)
- Provider health monitoring
- Error rate tracking
- Event throughput metrics

From the PLAN:
"OpenTelemetry trace，记录 ASR final、LLM first token、TTS first byte、
audio playback、interrupt 等时间点；日志自动脱敏"
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class SpanKind(StrEnum):
    ASR = "asr"
    LLM = "llm"
    TTS = "tts"
    AUDIO_PLAYBACK = "audio_playback"
    INTERRUPT = "interrupt"
    MEMORY_QUERY = "memory_query"
    REFLECTION = "reflection"


@dataclass
class LatencySpan:
    """A single timing span in the conversation pipeline."""

    kind: SpanKind
    turn_id: str
    start_time: float
    end_time: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if self.end_time is None:
            return int((time.time() - self.start_time) * 1000)
        return int((self.end_time - self.start_time) * 1000)

    def finish(self, **attrs: Any) -> None:
        self.end_time = time.time()
        self.attributes.update(attrs)


@dataclass
class TurnTrace:
    """A complete trace for a single conversation turn."""

    turn_id: str
    session_id: str
    spans: dict[SpanKind, LatencySpan] = field(default_factory=dict)
    interrupted: bool = False
    created_at: float = field(default_factory=time.time)

    def start_span(self, kind: SpanKind, **attrs: Any) -> LatencySpan:
        span = LatencySpan(
            kind=kind, turn_id=self.turn_id, start_time=time.time(), attributes=dict(attrs)
        )
        self.spans[kind] = span
        return span

    def get_e2e_latency_ms(self) -> int:
        """End-to-end latency: earliest start → latest end."""
        if not self.spans:
            return 0
        starts = [s.start_time for s in self.spans.values()]
        ends = [s.end_time for s in self.spans.values() if s.end_time is not None]
        if not ends:
            return 0
        return int((max(ends) - min(starts)) * 1000)


@dataclass
class LatencyStats:
    """Aggregated latency statistics."""

    count: int = 0
    values: list[int] = field(default_factory=list)

    def record(self, value_ms: int) -> None:
        self.count += 1
        self.values.append(value_ms)
        # Keep last 1000 values
        if len(self.values) > 1000:
            self.values = self.values[-1000:]

    @property
    def p50(self) -> float:
        return self._percentile(50)

    @property
    def p95(self) -> float:
        return self._percentile(95)

    @property
    def p99(self) -> float:
        return self._percentile(99)

    @property
    def avg(self) -> float:
        if not self.values:
            return 0.0
        return sum(self.values) / len(self.values)

    def _percentile(self, p: float) -> float:
        if not self.values:
            return 0.0
        sorted_vals = sorted(self.values)
        idx = int(len(sorted_vals) * p / 100.0)
        idx = min(idx, len(sorted_vals) - 1)
        return sorted_vals[idx]


class TelemetryService:
    """Manages observability: traces, latency stats, and health monitoring."""

    def __init__(self) -> None:
        # Active traces
        self._active_traces: dict[str, TurnTrace] = {}

        # Latency aggregation by span kind
        self._latency: dict[str, LatencyStats] = defaultdict(LatencyStats)

        # E2E latency aggregation
        self._e2e_latency = LatencyStats()

        # Error counters
        self._errors: dict[str, int] = defaultdict(int)

        # Event throughput
        self._event_counts: dict[str, int] = defaultdict(int)
        self._last_throughput_reset: float = time.time()

        # Provider health snapshots
        self._provider_health: dict[str, str] = {}

    # ── Trace management ──────────────────────────────────────────────

    def start_turn_trace(self, turn_id: str, session_id: str) -> TurnTrace:
        """Begin tracing a new conversation turn."""
        trace = TurnTrace(turn_id=turn_id, session_id=session_id)
        self._active_traces[turn_id] = trace
        return trace

    def get_trace(self, turn_id: str) -> TurnTrace | None:
        return self._active_traces.get(turn_id)

    def finish_turn_trace(self, turn_id: str, interrupted: bool = False) -> TurnTrace | None:
        """Complete a turn trace and record its latencies."""
        trace = self._active_traces.pop(turn_id, None)
        if not trace:
            return None

        trace.interrupted = interrupted

        # Record per-span latencies
        for kind, span in trace.spans.items():
            if span.duration_ms > 0:
                self._latency[kind.value].record(span.duration_ms)

        # Record E2E latency
        e2e = trace.get_e2e_latency_ms()
        if e2e > 0:
            self._e2e_latency.record(e2e)

        return trace

    # ── Span recording ────────────────────────────────────────────────

    def record_asr(self, turn_id: str, duration_ms: int, provider: str = "unknown") -> None:
        trace = self._active_traces.get(turn_id)
        if trace:
            span = trace.spans.get(SpanKind.ASR)
            if span:
                span.finish(provider=provider)
                return  # Latency recorded via span.finish() → TurnTrace.get_e2e_latency_ms()
        # Only record standalone if no active trace context
        self._latency["asr"].record(duration_ms)

    def record_llm(self, turn_id: str, ttft_ms: int, total_ms: int, model: str = "unknown") -> None:
        trace = self._active_traces.get(turn_id)
        if trace:
            span = trace.spans.get(SpanKind.LLM)
            if span:
                span.finish(ttft_ms=ttft_ms, total_ms=total_ms, model=model)
                return  # Latency recorded via span → trace
        self._latency["llm_ttft"].record(ttft_ms)
        self._latency["llm_total"].record(total_ms)

    def record_tts(self, turn_id: str, ttfb_ms: int, provider: str = "unknown") -> None:
        trace = self._active_traces.get(turn_id)
        if trace:
            span = trace.spans.get(SpanKind.TTS)
            if span:
                span.finish(provider=provider)
                return  # Latency recorded via span → trace
        self._latency["tts"].record(ttfb_ms)

    def record_audio_playback(
        self, turn_id: str, duration_ms: int, interrupted: bool = False
    ) -> None:
        if interrupted:
            self._latency["interrupt"].record(duration_ms)

    # ── Error tracking ────────────────────────────────────────────────

    def record_error(self, component: str, error_type: str) -> None:
        key = f"{component}:{error_type}"
        self._errors[key] += 1

    def get_error_counts(self) -> dict[str, int]:
        return dict(self._errors)

    # ── Event throughput ──────────────────────────────────────────────

    def record_event(self, event_type: str) -> None:
        self._event_counts[event_type] += 1

    def get_throughput_per_second(self) -> float:
        elapsed = time.time() - self._last_throughput_reset
        if elapsed <= 0:
            return 0.0
        total = sum(self._event_counts.values())
        return total / elapsed

    # ── Provider health ───────────────────────────────────────────────

    def record_provider_health(self, provider_name: str, health: str) -> None:
        self._provider_health[provider_name] = health

    def get_provider_health(self) -> dict[str, str]:
        return dict(self._provider_health)

    # ── Aggregate reports ─────────────────────────────────────────────

    def get_latency_report(self) -> dict[str, Any]:
        """Get a complete latency report for all pipeline stages."""
        report = {"e2e": self._e2e_report()}
        for kind, stats in self._latency.items():
            report[kind] = {
                "count": stats.count,
                "p50_ms": stats.p50,
                "p95_ms": stats.p95,
                "p99_ms": stats.p99,
                "avg_ms": stats.avg,
            }
        return report

    def _e2e_report(self) -> dict[str, Any]:
        stats = self._e2e_latency
        return {
            "count": stats.count,
            "p50_ms": stats.p50,
            "p95_ms": stats.p95,
            "p99_ms": stats.p99,
            "avg_ms": stats.avg,
        }

    def get_health_report(self) -> dict[str, Any]:
        """Get a comprehensive health report."""
        return {
            "providers": self.get_provider_health(),
            "errors": self.get_error_counts(),
            "latency": self.get_latency_report(),
            "throughput_per_second": self.get_throughput_per_second(),
            "latency_ok": self._check_latency_targets(),
        }

    def _check_latency_targets(self) -> dict[str, bool | None]:
        """Check if latency meets PLAN targets."""
        targets = {
            "e2e_p50": ("e2e", 50, 900),
            "e2e_p95": ("e2e", 95, 1800),
            "asr_p50": ("asr", 50, 300),
            "llm_ttft_p50": ("llm_ttft", 50, 500),
            "tts_p50": ("tts", 50, 300),
        }
        results: dict[str, bool | None] = {}
        for name, (kind, pct, target_ms) in targets.items():
            stats = self._latency.get(kind)
            if stats and stats.values:
                actual = stats.p50 if pct == 50 else stats.p95
                results[name] = actual <= target_ms
            else:
                results[name] = None  # No data is unknown, never evidence of passing.
        return results

    # ── Reset ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        self._active_traces.clear()
        self._latency.clear()
        self._e2e_latency = LatencyStats()
        self._errors.clear()
        self._event_counts.clear()
        self._last_throughput_reset = time.time()
        self._provider_health.clear()

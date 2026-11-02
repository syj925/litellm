from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Final

import pytest

from litellm.proxy.retail.request_tracing import (
    TraceCollector,
    activate_trace,
    bind_call_id,
    current_trace,
    redact_body,
    reset_trace,
    safe_attributes,
    trace_phase,
)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def active() -> Iterator[tuple[TraceCollector, Clock]]:
    clock: Final = Clock()
    trace: Final = TraceCollector("POST", "/v1/messages", clock, datetime(2026, 1, 1, tzinfo=timezone.utc))
    token: Final = activate_trace(trace)
    yield trace, clock
    reset_trace(token)


def test_attempts_preserve_each_failure_and_upstream_identity(active: tuple[TraceCollector, Clock]) -> None:
    trace, clock = active
    first: Final = trace.start_attempt({"deployment_id": "a", "api_base_origin": "https://u:p@example.test/v1?k=secret"})
    assert first is not None
    clock.advance(0.1)
    first.event("response_headers", {"status_code": 503})
    first.finish(503, "ServiceUnavailableError", "upstream-a")
    second: Final = trace.start_attempt({"deployment_id": "b"})
    assert second is not None
    clock.advance(0.2)
    second.finish(200, upstream_request_id="upstream-b")
    trace.complete()
    attempts: Final = trace.snapshot()["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0] == {
        "attempt": 1, "started_ms": 0.0, "ended_ms": 100.0,
        "attributes": {"deployment_id": "a", "api_base_origin": "https://example.test"},
        "status_code": 503, "error_type": "ServiceUnavailableError", "upstream_request_id": "upstream-a",
        "events": [{"name": "response_headers", "at_ms": 100.0, "attributes": {"status_code": 503}}],
    }
    assert isinstance(attempts[1], dict) and attempts[1]["upstream_request_id"] == "upstream-b"
    assert attempts[1]["started_ms"] == 100.0 and attempts[1]["ended_ms"] == 300.0


def test_phase_errors_and_late_settlement_extend_sample_without_moving_response_end(
    active: tuple[TraceCollector, Clock],
) -> None:
    trace, clock = active
    bind_call_id("call-a")
    with pytest.raises(ValueError), trace_phase("route"):
        clock.advance(0.05)
        raise ValueError("private details")
    trace.milestone("response_complete")
    trace.complete()
    with trace_phase("billing.settle"):
        clock.advance(0.1)
    result: Final = trace.snapshot()
    assert result["call_id"] == "call-a"
    assert result["milestones"] == {"request_received": 0.0, "response_complete": 50.0}
    assert result["duration_ms"] == 150.0
    assert result["finished_at"] == "2026-01-01T00:00:00.150000+00:00"
    assert result["phases"] == [
        {"name": "route", "start_ms": 0.0, "end_ms": 50.0, "duration_ms": 50.0, "status": "error"},
        {"name": "billing.settle", "start_ms": 50.0, "end_ms": 150.0, "duration_ms": 100.0, "status": "ok"},
    ]


def test_many_stream_writes_are_aggregated_and_do_not_drop_later_billing_phases(
    active: tuple[TraceCollector, Clock],
) -> None:
    trace, clock = active
    for _ in range(1000):
        started: Final = trace.offset_ms()
        clock.advance(0.001)
        trace.phase("client.write", started, "ok")
    with trace_phase("billing.settle"):
        clock.advance(0.01)
    trace.complete()
    result: Final = trace.snapshot()
    assert result["truncated"] is False
    phases: Final = result["phases"]
    assert isinstance(phases, list) and len(phases) == 2
    assert isinstance(phases[1], dict) and phases[1]["samples"] == 1000
    assert phases[1]["duration_ms"] == 1000.0


def test_milestones_are_first_observation_only(active: tuple[TraceCollector, Clock]) -> None:
    trace, clock = active
    trace.milestone("first_model_output")
    clock.advance(10)
    trace.milestone("first_model_output")
    trace.milestone("first_text_output")
    assert trace.snapshot()["milestones"] == {
        "request_received": 0.0, "first_model_output": 0.0, "first_text_output": 10000.0,
    }


def test_native_phase_hooks_also_collect_local_trace(active: tuple[TraceCollector, Clock]) -> None:
    from litellm.integrations.otel.runtime import phase_attributes, phase_event, phase_span

    trace, clock = active
    with phase_span("auth"):
        clock.advance(0.01)
        phase_event("auth.resolved", {"user_id": "user-a"})
        phase_attributes({"model": "sample", "api_key": "private"})
    result: Final = trace.snapshot()
    assert result["attributes"] == {"model": "sample"}
    assert result["events"] == [{"name": "auth.resolved", "at_ms": 10.0, "attributes": {"user_id": "user-a"}}]
    assert result["phases"] == [
        {"name": "auth", "start_ms": 0.0, "end_ms": 10.0, "duration_ms": 10.0, "status": "ok"},
    ]


def test_trace_redacts_nested_secrets_and_omits_sensitive_event_payloads() -> None:
    assert redact_body({"messages": [{"content": "use sk-not-a-real-secret123"}], "api_key": "private"}) == {
        "messages": [{"content": "use [REDACTED]"}], "api_key": "[REDACTED]",
    }
    assert safe_attributes({
        "authorization": "Bearer private", "headers": "private", "prompt": "private",
        "model": "sample", "duration_ms": float("nan"), "status_code": 503,
    }) == {"model": "sample", "status_code": 503}


def test_no_active_request_is_noop_and_context_is_restored() -> None:
    assert current_trace() is None
    with trace_phase("untraced"):
        bind_call_id("unused")
    assert current_trace() is None

from __future__ import annotations

import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from math import isfinite
from time import perf_counter
from typing import Final, TypeAlias
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import JsonValue

Scalar: TypeAlias = str | int | float | bool | None
_SECRET: Final = re.compile(
    r"authorization|cookie|password|secret|api.?key|access.?token|refresh.?token|private.?key|^token$", re.I,
)
_CONTENT: Final = re.compile(r"^(messages|prompt|input|content|body|headers|request_kwargs|response)$", re.I)
_TOKEN: Final = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+[^\s\"']+|\bAIza[A-Za-z0-9_-]{20,}", re.I)
_MAX_EVENTS: Final = 256
_MAX_PHASES: Final = 128
_MAX_ATTEMPTS: Final = 32
_ACTIVE: Final[ContextVar[TraceCollector | None]] = ContextVar("retail_request_trace", default=None)


def safe_origin(value: str) -> str:
    try:
        url: Final = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            return ""
        host: Final = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        authority: Final = f"{host}:{url.port}" if url.port else host
        return urlunsplit((url.scheme, authority, "", "", ""))
    except ValueError:
        return ""


def _safe_scalar(key: str, value: Scalar) -> JsonValue:
    if isinstance(value, str):
        return (
            safe_origin(value) if key in {"api_base", "api_base_origin", "url"}
            else _TOKEN.sub("[REDACTED]", value)[:512]
        )
    return value


def safe_attributes(values: Mapping[str, object]) -> Mapping[str, JsonValue]:
    return {
        key[:96]: _safe_scalar(key, value)
        for key, value in values.items()
        if isinstance(value, (str, int, float, bool, type(None)))
        and not _SECRET.search(key) and not _CONTENT.search(key)
        and (not isinstance(value, float) or isfinite(value))
    }


def redact_body(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 20:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET.search(key) else redact_body(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_body(item, depth + 1) for item in value]
    return _TOKEN.sub("[REDACTED]", value) if isinstance(value, str) else value


class AttemptCollector:
    def __init__(self, trace: TraceCollector, attempt: int, attributes: Mapping[str, object]) -> None:
        self._trace = trace
        self._attempt = attempt
        self._started_ms = trace.offset_ms()
        self._ended_ms: float | None = None
        self._attributes = safe_attributes(attributes)
        self._events: tuple[Mapping[str, JsonValue], ...] = ()
        self._status_code: int | None = None
        self._error_type: str | None = None
        self._upstream_request_id: str | None = None

    def event(self, name: str, attributes: Mapping[str, object] | None = None) -> None:
        if len(self._events) >= _MAX_EVENTS:
            self._trace.truncate()
            return
        self._events = (*self._events, {
            "name": name[:96], "at_ms": self._trace.offset_ms(),
            "attributes": safe_attributes(attributes or {}),
        })

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        self._attributes = {**self._attributes, **safe_attributes(attributes)}

    async def trace_httpx(self, name: str, info: Mapping[str, object]) -> None:
        error: Final = info.get("exception")
        self.event(name, {"error_type": type(error).__name__} if error is not None else {})

    def finish(
        self, status_code: int | None = None, error_type: str | None = None,
        upstream_request_id: str | None = None,
    ) -> None:
        self._ended_ms = self._trace.offset_ms()
        if status_code is not None:
            self._status_code = status_code
        if error_type is not None:
            self._error_type = error_type[:128]
        if upstream_request_id is not None:
            self._upstream_request_id = _TOKEN.sub("[REDACTED]", upstream_request_id)[:256]

    def snapshot(self) -> Mapping[str, JsonValue]:
        return {
            "attempt": self._attempt, "started_ms": self._started_ms, "ended_ms": self._ended_ms,
            "attributes": dict(self._attributes), "status_code": self._status_code,
            "error_type": self._error_type, "upstream_request_id": self._upstream_request_id,
            "events": [dict(event) for event in self._events],
        }


class TraceCollector:
    def __init__(
        self, method: str, path: str, clock: Callable[[], float] = perf_counter,
        started_at: datetime | None = None,
    ) -> None:
        self._clock = clock
        self._started = clock()
        self.trace_id = str(uuid4())
        self.call_id: str | None = None
        self.retail_request_id: str | None = None
        self._started_at = started_at or datetime.now(timezone.utc)
        self._duration_ms: float | None = None
        self._last_ms = 0.0
        self._method = method
        self._path = path.split("?", 1)[0][:512]
        self._attributes: Mapping[str, JsonValue] = {}
        self._milestones: Mapping[str, JsonValue] = {"request_received": 0.0}
        self._events: tuple[Mapping[str, JsonValue], ...] = ()
        self._phases: tuple[Mapping[str, JsonValue], ...] = ()
        self._write_start_ms: float | None = None
        self._write_end_ms = 0.0
        self._write_total_ms = 0.0
        self._write_count = 0
        self._attempts: tuple[AttemptCollector, ...] = ()
        self._truncated = False
        self.request_body_bytes = 0
        self.response_status: int | None = None

    def offset_ms(self) -> float:
        self._last_ms = round(max(self._last_ms, (self._clock() - self._started) * 1000), 3)
        return self._last_ms

    def truncate(self) -> None:
        self._truncated = True

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        self._attributes = {**self._attributes, **safe_attributes(attributes)}

    def milestone(self, name: str) -> None:
        if name not in self._milestones and len(self._milestones) < _MAX_EVENTS:
            self._milestones = {**self._milestones, name: self.offset_ms()}

    def event(self, name: str, attributes: Mapping[str, object] | None = None) -> None:
        if len(self._events) >= _MAX_EVENTS:
            self.truncate()
            return
        self._events = (*self._events, {
            "name": name[:96], "at_ms": self.offset_ms(), "attributes": safe_attributes(attributes or {}),
        })

    def phase(self, name: str, started_ms: float, status: str) -> None:
        ended_ms: Final = self.offset_ms()
        if name == "client.write":
            self._write_start_ms = started_ms if self._write_start_ms is None else self._write_start_ms
            self._write_end_ms = ended_ms
            self._write_total_ms += max(0.0, ended_ms - started_ms)
            self._write_count += 1
            return
        if len(self._phases) >= _MAX_PHASES:
            self.truncate()
            return
        self._phases = (*self._phases, {
            "name": name[:96], "start_ms": started_ms, "end_ms": ended_ms,
            "duration_ms": round(max(0.0, ended_ms - started_ms), 3), "status": status,
        })

    def start_attempt(self, attributes: Mapping[str, object]) -> AttemptCollector | None:
        if len(self._attempts) >= _MAX_ATTEMPTS:
            self.truncate()
            return None
        attempt: Final = AttemptCollector(self, len(self._attempts) + 1, attributes)
        self._attempts = (*self._attempts, attempt)
        self.milestone("upstream_started")
        return attempt

    def complete(self) -> None:
        self._duration_ms = self.offset_ms()

    def snapshot(self) -> Mapping[str, JsonValue]:
        elapsed: Final = max(self._duration_ms or 0.0, self._last_ms)
        writes: Final[tuple[Mapping[str, JsonValue], ...]] = ({
            "name": "client.write", "start_ms": self._write_start_ms, "end_ms": self._write_end_ms,
            "duration_ms": round(self._write_total_ms, 3), "samples": self._write_count, "status": "ok",
        },) if self._write_count else ()
        return {
            "schema_version": 1, "trace_id": self.trace_id, "call_id": self.call_id,
            "retail_request_id": self.retail_request_id,
            "started_at": self._started_at.isoformat(),
            "finished_at": (self._started_at + timedelta(milliseconds=elapsed)).isoformat()
            if self._duration_ms is not None else None,
            "duration_ms": elapsed, "method": self._method, "path": self._path,
            "request_body_bytes": self.request_body_bytes, "response_status": self.response_status,
            "attributes": dict(self._attributes), "milestones": dict(self._milestones),
            "events": [dict(event) for event in self._events],
            "phases": [dict(phase) for phase in (*self._phases, *writes)],
            "attempts": [dict(attempt.snapshot()) for attempt in self._attempts], "truncated": self._truncated,
        }


def current_trace() -> TraceCollector | None:
    return _ACTIVE.get()


def activate_trace(trace: TraceCollector) -> Token[TraceCollector | None]:
    return _ACTIVE.set(trace)


def reset_trace(token: Token[TraceCollector | None]) -> None:
    _ACTIVE.reset(token)


def bind_call_id(call_id: str) -> None:
    trace: Final = current_trace()
    if trace is not None and call_id:
        trace.call_id = call_id[:191]


def bind_retail_request_id(request_id: str) -> None:
    trace: Final = current_trace()
    if trace is not None:
        trace.retail_request_id = request_id[:191]


def record_event(name: str, attributes: Mapping[str, object] | None = None) -> None:
    trace: Final = current_trace()
    if trace is not None:
        trace.event(name, attributes)


def start_attempt(attributes: Mapping[str, object]) -> AttemptCollector | None:
    trace: Final = current_trace()
    return trace.start_attempt(attributes) if trace is not None else None


@contextmanager
def trace_phase(name: str) -> Generator[None]:
    trace: Final = current_trace()
    if trace is None:
        yield None
        return
    started: Final = trace.offset_ms()
    try:
        yield None
    except BaseException:
        trace.phase(name, started, "error")
        raise
    else:
        trace.phase(name, started, "ok")

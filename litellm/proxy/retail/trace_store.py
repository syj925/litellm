import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, JsonValue

from .models import nanos_to_amount
from .store import WalletStore, _many, _one, get_store

BODY_RETENTION: Final = timedelta(minutes=30)
TRACE_RETENTION_DAYS: Final = 30
_MAX_BODY_BYTES: Final = 65_536
_SENSITIVE_KEY: Final = re.compile(
    r"(?:^token$|authorization|cookie|password|secret|credential|private[_-]?key|api[_-]?key|"
    r"(?:auth|session|id|access|refresh)[_-]?token)", re.IGNORECASE
)
_BEARER: Final = re.compile(r"\bBearer\s+[^\s,;\"']+", re.IGNORECASE)
_KEY: Final = re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_-]{12,}\b")


class _TraceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    call_id: str | None
    retail_request_id: str | None
    trace: Mapping[str, JsonValue]
    request_body: JsonValue = None
    body_expires_at: datetime


class _BillingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    status: str
    reserved_nanos: int
    settled_nanos: int | None
    price_snapshot: Mapping[str, JsonValue]
    details: Mapping[str, JsonValue]


class _LedgerTraceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    amount_nanos: int
    created_at: datetime
    details: Mapping[str, JsonValue]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _redact_text(value: str) -> str:
    return _KEY.sub("[REDACTED]", _BEARER.sub("Bearer [REDACTED]", value))


def redact_trace_value(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 16:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(key) else redact_trace_value(item, depth + 1)
            for key, item in tuple(value.items())[:256]
        }
    if isinstance(value, list):
        return [redact_trace_value(item, depth + 1) for item in value[:256]]
    if isinstance(value, str):
        return _redact_text(value[:16_384])
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _body_value(value: JsonValue) -> JsonValue:
    redacted: Final = redact_trace_value(value)
    if len(json.dumps(redacted, ensure_ascii=False).encode("utf-8")) > _MAX_BODY_BYTES:
        return {"truncated": True, "reason": "Request body exceeds the diagnostic storage limit"}
    return redacted


def _started_at(snapshot: Mapping[str, JsonValue], now: datetime) -> datetime:
    value: Final = snapshot.get("started_at")
    if not isinstance(value, str):
        return now
    try:
        parsed: Final = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return now
    return min(_utc(parsed), now)


def _observed_until(snapshot: Mapping[str, JsonValue]) -> float:
    duration: Final = snapshot.get("duration_ms")
    return float(duration) if isinstance(duration, (float, int)) and math.isfinite(duration) and duration >= 0 else 0.0


async def cleanup_trace_bodies(store: WalletStore | None = None) -> None:
    storage: Final = store if store is not None else get_store()
    await storage.initialize()
    async with storage.transaction() as connection:
        await connection.execute(
            """UPDATE retail_request_traces SET request_body=NULL
            WHERE body_expires_at<=now() AND request_body IS NOT NULL"""
        )
        await connection.execute(
            "DELETE FROM retail_request_traces WHERE created_at<now()-(%s * interval '1 day')",
            (TRACE_RETENTION_DAYS,),
        )


async def persist_trace(
    snapshot: Mapping[str, JsonValue], request_body: JsonValue = None,
    retail_request_id: str | None = None, *, store: WalletStore | None = None,
) -> None:
    native_call_id: Final = snapshot.get("call_id")
    supplied_trace_id: Final = snapshot.get("trace_id")
    trace_id: Final = supplied_trace_id if isinstance(supplied_trace_id, str) and supplied_trace_id else native_call_id
    if not isinstance(trace_id, str) or not 0 < len(trace_id) <= 191:
        return
    call_id: Final = native_call_id if isinstance(native_call_id, str) and 0 < len(native_call_id) <= 191 else None
    storage: Final = store if store is not None else get_store()
    await storage.initialize()
    now: Final = datetime.now(timezone.utc)
    expires_at: Final = _started_at(snapshot, now) + BODY_RETENTION
    frozen_snapshot: Final = redact_trace_value(dict(snapshot))
    request_id: Final = retail_request_id or snapshot.get("retail_request_id")
    bound_request_id: Final = request_id if isinstance(request_id, str) and 0 < len(request_id) <= 191 else None
    body: Final = _body_value(request_body) if expires_at > now else None
    async with storage.transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_request_traces
            (trace_id,call_id,retail_request_id,trace,request_body,body_expires_at,recorded_until_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(trace_id) DO UPDATE SET
            retail_request_id=COALESCE(EXCLUDED.retail_request_id,retail_request_traces.retail_request_id),
            call_id=COALESCE(EXCLUDED.call_id,retail_request_traces.call_id),
            trace=CASE WHEN retail_request_traces.recorded_until_ms<=EXCLUDED.recorded_until_ms
              THEN EXCLUDED.trace ELSE retail_request_traces.trace END,
            request_body=CASE WHEN retail_request_traces.body_expires_at>now()
              THEN COALESCE(retail_request_traces.request_body,EXCLUDED.request_body) ELSE NULL END,
            recorded_until_ms=GREATEST(retail_request_traces.recorded_until_ms,EXCLUDED.recorded_until_ms),
            updated_at=now()""",
            (trace_id, call_id, bound_request_id,
             Jsonb(frozen_snapshot), Jsonb(body) if body is not None else None,
             expires_at, _observed_until(snapshot)),
        )


def _money(value: JsonValue) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        amount: Final = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(amount, "f") if amount.is_finite() and amount >= 0 else None


def _mapping_value(value: JsonValue) -> Mapping[str, JsonValue] | None:
    return value if isinstance(value, dict) else None


def _object_json(value: JsonValue) -> JsonValue:
    return value if isinstance(value, dict) else None


def _billing_view(row: _BillingRow, ledger: tuple[_LedgerTraceRow, ...]) -> Mapping[str, JsonValue]:
    settled: Final = next((entry.details for entry in reversed(ledger) if entry.kind == "charge"), {})
    details: Final = _mapping_value(redact_trace_value({**row.details, **settled})) or {}
    provider_cost: Final = _money(details.get("provider_cost"))
    snapshot: Final = _mapping_value(redact_trace_value(dict(row.price_snapshot))) or {}
    resource: Final[Mapping[str, JsonValue]] = {
        "pool_id": snapshot.get("resource_pool_id"),
        "pool_name": snapshot.get("resource_pool_name"),
        "source_kind": snapshot.get("resource_source_kind"),
        "routing_settings_version": snapshot.get("routing_settings_version"),
    }
    return {
        "retail_request_id": row.request_id,
        "status": row.status,
        "reserved_amount": nanos_to_amount(row.reserved_nanos),
        "settled_amount": nanos_to_amount(row.settled_nanos) if row.settled_nanos is not None else None,
        "currency": "USD",
        "price_snapshot": dict(snapshot) if snapshot else None,
        "usage": _object_json(details.get("usage")),
        "cost_breakdown": _object_json(details.get("cost_breakdown")),
        "expression_pricing": _object_json(details.get("expression_pricing")),
        "provider_cost": provider_cost,
        "provider_cost_source": "gateway_cost_calculation" if provider_cost is not None else None,
        "ledger": [
            {"id": entry.id, "kind": entry.kind, "amount": nanos_to_amount(entry.amount_nanos),
             "created_at": _iso(entry.created_at)}
            for entry in ledger
        ],
        "resource": dict(resource),
    }


def trace_response(
    call_id: str, trace: _TraceRow | None, billing: _BillingRow | None,
    ledger: tuple[_LedgerTraceRow, ...], now: datetime, ambiguous_call_id: bool = False,
) -> Mapping[str, JsonValue]:
    body_available: Final = (
        trace is not None and trace.request_body is not None and _utc(trace.body_expires_at) > _utc(now)
    )
    bill: Final = _billing_view(billing, ledger) if billing is not None else None
    missing: Final = tuple(
        name for name, absent in (
            ("trace", trace is None), ("request_body", not body_available), ("billing", bill is None),
            ("frozen_pricing", bill is None or bill["price_snapshot"] is None),
            ("usage", bill is None or bill["usage"] is None),
            ("provider_cost", bill is None or bill["provider_cost"] is None),
            ("native_log_link", trace is not None and trace.call_id is None),
            ("ambiguous_call_id", ambiguous_call_id),
        ) if absent
    )
    return {
        "call_id": call_id,
        "recorded": trace is not None,
        "trace": dict(trace.trace) if trace is not None else None,
        "request_body": {
            "available": body_available,
            "expires_at": _iso(trace.body_expires_at) if trace is not None else None,
            "value": trace.request_body if trace is not None and body_available else None,
        },
        "billing": dict(bill) if bill is not None else None,
        "missing": list(missing),
    }


async def get_request_trace(store: WalletStore, call_id: str) -> Mapping[str, JsonValue]:
    await cleanup_trace_bodies(store)
    async with store.transaction() as connection:
        traces: Final = await _many(
            connection,
            """SELECT trace_id,call_id,retail_request_id,trace,
            CASE WHEN body_expires_at>now() THEN request_body END AS request_body,body_expires_at
            FROM retail_request_traces WHERE call_id=%s OR trace_id=%s
            ORDER BY CASE WHEN trace_id=%s THEN 0 ELSE 1 END,created_at DESC LIMIT 2""",
            (call_id, call_id, call_id), _TraceRow,
        )
        trace: Final = traces[0] if traces else None
        retail_id: Final = trace.retail_request_id if trace is not None else None
        gateway_call_id: Final = call_id if trace is None else None
        billing: Final = await _one(
            connection,
            """SELECT request_id,status,reserved_nanos,settled_nanos,price_snapshot,details
            FROM retail_reservations WHERE request_id=%s
            OR (details ? 'gateway_call_id' AND details->>'gateway_call_id'=%s)
            ORDER BY CASE WHEN request_id=%s THEN 0 ELSE 1 END,created_at DESC LIMIT 1""",
            (retail_id, gateway_call_id, retail_id), _BillingRow,
        )
        ledger: Final = tuple(await _many(
            connection,
            """SELECT id::text,kind,amount_nanos,created_at,details FROM retail_ledger
            WHERE request_id=%s ORDER BY created_at,id""",
            (billing.request_id if billing is not None else None,), _LedgerTraceRow,
        ))
    ambiguous: Final = len(traces) > 1 and trace is not None and trace.trace_id != call_id
    return trace_response(call_id, trace, billing, ledger, datetime.now(timezone.utc), ambiguous)

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from json import dumps
from pathlib import Path
from typing import Final
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, JsonValue

from .models import IdempotencyConflict
from .store import _many, _one

_ALLOWED_ACTIONS: Final = frozenset(
    (
        "organization.created",
        "organization.updated",
        "organization.member_associated",
        "organization.member_updated",
        "report.created",
        "report.completed",
        "report.failed",
        "object_metadata.updated",
        "model_budget.created",
        "model_budget.updated",
        "model_budget.scheduled",
        "monthly_budget.created",
        "monthly_budget.updated",
        "monthly_budget.stopped",
        "billing_organization.updated",
        "organization_report.created",
        "organization_report.completed",
        "organization_report.failed",
        "finance_grant.updated",
        "delegated_report.created",
        "delegated_report.completed",
        "delegated_report.failed",
        "audit_delivery.updated",
        "audit_delivery.retried",
        "audit_delivery.discarded",
        "native_key.rotated",
        "native_password.changed",
        "native_session.revoked",
        "native_identity.configured",
        "native_identity.signed_in",
        "native_api_identity.configured",
        "native_scim.configured",
        "native_scim.user_created",
        "native_scim.user_updated",
        "native_scim.user_deleted",
        "native_scim.group_created",
        "native_scim.group_updated",
        "native_scim.group_deleted",
        "native_request_policy.prompt_created",
        "native_request_policy.prompt_updated",
        "native_request_policy.prompt_deleted",
        "native_request_policy.policy_created",
        "native_request_policy.policy_updated",
        "native_request_policy.policy_deleted",
        "native_request_policy.binding_created",
        "native_request_policy.binding_updated",
        "native_request_policy.binding_deleted",
    )
)
_ALLOWED_FIELDS: Final = frozenset(
    (
        "name",
        "enabled",
        "role",
        "user_id",
        "start",
        "end",
        "currency",
        "row_count",
        "format",
        "state",
        "kind",
        "tag_count",
        "model",
        "limit_usd",
        "predecessor_id",
        "organization_id",
        "can_view",
        "can_export",
    )
)


class OperationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    organization_id: str | None
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    result: str
    version_before: int | None
    version_after: int | None
    changes: Mapping[str, JsonValue]
    created_at: datetime


class _SavedOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    request_digest: str
    response: Mapping[str, JsonValue]


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    schema: Final = Path(__file__).with_name("operation_events_schema.sql").read_text(encoding="utf-8")
    await connection.execute(schema, prepare=False)


def operation_digest(action: str, payload: Mapping[str, JsonValue]) -> str:
    return sha256(
        dumps({"action": action, "payload": dict(payload)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def replay_operation(
    connection: psycopg.AsyncConnection, actor_id: str, key: str, action: str, digest: str
) -> Mapping[str, JsonValue] | None:
    if not key or len(key) > 128:
        raise HTTPException(422, "A valid idempotency key is required")
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:operation:" + actor_id + ":" + key,)
    )
    existing: Final = await _one(
        connection,
        "SELECT action,request_digest,response FROM retail_operation_events WHERE actor_id=%s AND idempotency_key=%s",
        (actor_id, key),
        _SavedOperation,
    )
    if existing is None:
        return None
    if existing.action != action or existing.request_digest != digest:
        raise IdempotencyConflict("The idempotency key was already used for another operation")
    return existing.response


def safe_changes(changes: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if set(changes) - _ALLOWED_FIELDS:
        raise ValueError("Audit changes contain unsupported fields")
    if len(dumps(changes)) > 4096:
        raise ValueError("Audit changes exceed the event size limit")
    if any(isinstance(value, (dict, list)) for value in changes.values()):
        raise ValueError("Audit changes accept scalar values only")
    return dict(changes)


async def append_operation_event(
    connection: psycopg.AsyncConnection,
    *,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    organization_id: str | None,
    version_before: int | None,
    version_after: int | None,
    changes: Mapping[str, JsonValue],
    idempotency_key: str,
    request_digest: str,
    response: Mapping[str, JsonValue],
    occurred_at: datetime | None = None,
) -> None:
    if occurred_at is not None and occurred_at.utcoffset() is None:
        raise ValueError("Audit occurrence time requires an explicit timezone")
    if action not in _ALLOWED_ACTIONS or resource_type not in (
        "organization",
        "organization_member",
        "report",
        "object_metadata",
        "model_budget",
        "monthly_budget",
        "billing_organization",
        "organization_report",
        "finance_grant",
        "delegated_report",
        "audit_delivery",
        "native_operation",
    ):
        raise ValueError("Unsupported audit event type")
    await connection.execute(
        """INSERT INTO retail_operation_events
        (id,organization_id,actor_id,action,resource_type,resource_id,result,version_before,version_after,
         changes,idempotency_key,request_digest,response,created_at)
        VALUES(%s,%s,%s,%s,%s,%s,'succeeded',%s,%s,%s,%s,%s,%s,COALESCE(%s,clock_timestamp()))""",
        (
            str(uuid4()),
            organization_id,
            actor_id,
            action,
            resource_type,
            resource_id,
            version_before,
            version_after,
            Jsonb(safe_changes(changes)),
            idempotency_key,
            request_digest,
            Jsonb(dict(response)),
            occurred_at,
        ),
    )


def event_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    if (start is None) != (end is None):
        raise HTTPException(422, "Audit start and end must be supplied together")
    finish: Final = end if end is not None else datetime.now(timezone.utc)
    begin: Final = start if start is not None else finish - timedelta(days=31)
    if (
        begin.utcoffset() is None
        or finish.utcoffset() is None
        or not timedelta(0) < finish - begin <= timedelta(days=31)
    ):
        raise HTTPException(422, "Audit windows require explicit timezones and a positive range of at most 31 days")
    return begin, finish


async def list_operation_events(
    connection: psycopg.AsyncConnection,
    *,
    organization_id: str | None = None,
    action: str | None = None,
    actor_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 25,
    offset: int = 0,
) -> Mapping[str, JsonValue]:
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise HTTPException(422, "Invalid audit pagination")
    begin, finish = event_window(start, end)
    condition: Final = """ WHERE created_at>=%s AND created_at<%s
        AND (%s::text IS NULL OR organization_id=%s)
        AND (%s::text IS NULL OR action=%s) AND (%s::text IS NULL OR actor_id=%s)"""
    params: Final = (begin, finish, organization_id, organization_id, action, action, actor_id, actor_id)
    await connection.execute("SET LOCAL statement_timeout = '3000ms'")
    total: Final = await _one(
        connection, "SELECT count(*) AS total FROM retail_operation_events" + condition, params, _Count
    )
    rows: Final = await _many(
        connection,
        """SELECT id,organization_id,actor_id,action,resource_type,resource_id,result,version_before,version_after,
        changes,created_at FROM retail_operation_events"""
        + condition
        + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset),
        OperationEvent,
    )
    return {
        "items": [row.model_dump(mode="json") for row in rows],
        "total": total.total if total is not None else 0,
        "limit": limit,
        "offset": offset,
    }

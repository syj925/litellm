import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Final, Literal, TypeAlias
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StrictInt, field_validator

from .budget_accounting import lock_budget_wallet
from .budget_management import (
    budget_amount,
    configured_budget_models,
    exact_model_name,
    money_string,
    pagination,
    validate_user_id,
)
from .control_plane_access import ControlPlaneActor
from .models import amount_to_nanos
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_store import require_admin
from .store import WalletStore, _many, _one

MonthlyTimezone: TypeAlias = Literal["UTC", "Asia/Shanghai"]
_MONTH: Final = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z", re.ASCII)


def month_floor(at: datetime, zone: MonthlyTimezone) -> datetime:
    if at.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required")
    local: Final = at.astimezone(ZoneInfo(zone))
    return datetime(local.year, local.month, 1, tzinfo=local.tzinfo).astimezone(timezone.utc)


def following_month(at: datetime, zone: MonthlyTimezone) -> datetime:
    local: Final = month_floor(at, zone).astimezone(ZoneInfo(zone))
    return datetime(local.year + local.month // 12, local.month % 12 + 1, 1,
                    tzinfo=local.tzinfo).astimezone(timezone.utc)


def parse_month(value: str, zone: MonthlyTimezone) -> datetime:
    if not _MONTH.fullmatch(value) or not 1 <= int(value[:4]) <= 9998:
        raise ValueError("Enter a calendar month as YYYY-MM")
    return datetime(int(value[:4]), int(value[5:]), 1, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


class CreateMonthlyBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = Field(min_length=1, max_length=191)
    start_month: str
    timezone: MonthlyTimezone = "Asia/Shanghai"
    limit_usd: str = Field(min_length=1, max_length=30)
    currency: Literal["USD"] = "USD"

    @field_validator("model")
    @classmethod
    def exact_model(cls, value: str) -> str:
        if not exact_model_name(value):
            raise ValueError("Select one exact configured public model")
        return value

    @field_validator("start_month")
    @classmethod
    def calendar_start(cls, value: str) -> str:
        parse_month(value, "UTC")
        return value

    @field_validator("limit_usd")
    @classmethod
    def positive_limit(cls, value: str) -> str:
        return budget_amount(value)


class UpdateMonthlyBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)
    limit_usd: str = Field(min_length=1, max_length=30)

    @field_validator("limit_usd")
    @classmethod
    def positive_limit(cls, value: str) -> str:
        return budget_amount(value)


class StopMonthlyBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)


class MonthlyBudgetView(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    model: str
    currency: Literal["USD"] = "USD"
    timezone: MonthlyTimezone
    starts_at: AwareDatetime
    stops_at: AwareDatetime | None
    limit_usd: str
    limit_effective_from: AwareDatetime
    version: int
    state: Literal["scheduled", "active", "stopping", "stopped"]
    current_window_ready: bool


class _Plan(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    user_id: str
    model: str
    timezone: MonthlyTimezone
    starts_at: AwareDatetime
    stops_at: AwareDatetime | None
    version: int
    reconcile_after: AwareDatetime
    created_at: AwareDatetime
    created_by: str
    updated_at: AwareDatetime
    updated_by: str


class _View(_Plan):
    limit_nanos: int
    limit_effective_from: AwareDatetime
    observed_at: AwareDatetime
    current_window_ready: bool


class _Clock(BaseModel):
    model_config = ConfigDict(frozen=True)
    at: AwareDatetime


class _Term(BaseModel):
    model_config = ConfigDict(frozen=True)
    limit_nanos: int


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    user_id: str


_VIEW: Final = """SELECT r.*,t.limit_nanos,t.effective_from AS limit_effective_from,clock_timestamp() AS observed_at,
    EXISTS(SELECT 1 FROM retail_model_budget_occurrences o JOIN retail_model_budget_policies p ON p.id=o.policy_id
      WHERE o.recurrence_id=r.id AND p.enabled AND p.effective_from<=clock_timestamp() AND p.ends_at>clock_timestamp())
      AS current_window_ready
    FROM retail_model_budget_recurrences r JOIN LATERAL
      (SELECT limit_nanos,effective_from FROM retail_model_budget_recurrence_terms WHERE recurrence_id=r.id
       ORDER BY effective_from DESC LIMIT 1) t ON true"""


def monthly_view(row: _View, *, admin: bool) -> Mapping[str, JsonValue]:
    state: Final = ("stopped" if row.stops_at is not None and
                   (row.stops_at <= row.observed_at or row.stops_at == row.starts_at) else
                   "stopping" if row.stops_at is not None else "scheduled" if row.starts_at > row.observed_at else "active")
    value: Final = MonthlyBudgetView(
        id=row.id, model=row.model, timezone=row.timezone, starts_at=row.starts_at, stops_at=row.stops_at,
        limit_usd=money_string(row.limit_nanos), limit_effective_from=row.limit_effective_from,
        version=row.version, state=state, current_window_ready=row.current_window_ready,
    ).model_dump(mode="json")
    return {**value, **({"user_id": row.user_id, "created_at": row.created_at.isoformat(), "created_by": row.created_by,
                        "updated_at": row.updated_at.isoformat(), "updated_by": row.updated_by} if admin else {})}


class MonthlyBudgetStore:
    def __init__(self, store: WalletStore, *, models: Callable[[], tuple[str, ...]] = configured_budget_models) -> None:
        self.wallet = store
        self._models = models

    async def _clock(self, connection: psycopg.AsyncConnection) -> datetime:
        row: Final = await _one(connection, "SELECT clock_timestamp() AS at", (), _Clock)
        if row is None:
            raise HTTPException(503, "The monthly budget clock is unavailable")
        return row.at

    async def _read(self, connection: psycopg.AsyncConnection, user_id: str, identifier: UUID) -> _View:
        row: Final = await _one(connection, _VIEW + " WHERE r.id=%s AND r.user_id=%s", (identifier, user_id), _View)
        if row is None:
            raise HTTPException(404, "The requested monthly budget is unavailable")
        return row

    async def _settings(self, connection: psycopg.AsyncConnection) -> None:
        await connection.execute("SET LOCAL statement_timeout='3000ms'")
        await connection.execute("SET LOCAL lock_timeout='500ms'")

    async def get(self, user_id: str, identifier: UUID, actor: ControlPlaneActor, *, admin: bool = True) -> Mapping[str, JsonValue]:
        if admin:
            require_admin(actor)
        elif actor.user_id != user_id:
            raise HTTPException(404, "The requested monthly budget is unavailable")
        validate_user_id(user_id)
        async with self.wallet.diagnostic_transaction() as connection:
            await self._settings(connection)
            return monthly_view(await self._read(connection, user_id, identifier), admin=admin)

    async def list(self, user_id: str, actor: ControlPlaneActor, *, admin: bool = True,
                   q: str = "", limit: int = 25, offset: int = 0) -> Mapping[str, JsonValue]:
        if admin:
            require_admin(actor)
        elif actor.user_id != user_id:
            raise HTTPException(404, "The requested monthly budget is unavailable")
        validate_user_id(user_id)
        pagination(q, limit, offset)
        pattern: Final = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.wallet.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await self._settings(connection)
            total: Final = await _one(connection, "SELECT count(*) AS total FROM retail_model_budget_recurrences WHERE user_id=%s AND model ILIKE %s",
                                      (user_id, pattern), _Count)
            rows: Final = await _many(connection, _VIEW + " WHERE r.user_id=%s AND r.model ILIKE %s ORDER BY r.created_at DESC,r.id LIMIT %s OFFSET %s",
                                      (user_id, pattern, limit, offset), _View)
        return {"items": [dict(monthly_view(row, admin=admin)) for row in rows],
                "total": total.total if total else 0, "limit": limit, "offset": offset}

    async def _audit(self, connection: psycopg.AsyncConnection, row: _View, actor: ControlPlaneActor, key: str,
                     action: str, digest: str, previous: int | None) -> Mapping[str, JsonValue]:
        response: Final = monthly_view(row, admin=True)
        await append_operation_event(connection, actor_id=actor.user_id, action=action, resource_type="monthly_budget",
            resource_id=str(row.id), organization_id=None, version_before=previous, version_after=row.version,
            changes={"user_id": row.user_id, "model": row.model, "limit_usd": money_string(row.limit_nanos),
                     "start": row.starts_at.isoformat(), "end": row.stops_at.isoformat() if row.stops_at else None,
                     "enabled": row.stops_at is None, "currency": "USD"},
            idempotency_key=key, request_digest=digest, response=dict(response))
        return response

    async def create(self, user_id: str, data: CreateMonthlyBudget, actor: ControlPlaneActor, key: str) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "monthly_budget.created"
        digest: Final = operation_digest(action, {"user_id": user_id, **data.model_dump(mode="json")})
        try:
            async with self.wallet.diagnostic_transaction() as connection:
                await self._settings(connection)
                await lock_budget_wallet(connection, user_id)
                replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
                if replay is not None:
                    return replay
                if data.model not in self._models():
                    raise HTTPException(422, "Select one current configured public model")
                now: Final = await self._clock(connection)
                start: Final = parse_month(data.start_month, data.timezone)
                if start < following_month(now, data.timezone):
                    raise HTTPException(422, "The first window must start next calendar month or later")
                conflict: Final = await _one(connection, """SELECT count(*) AS total FROM retail_model_budget_policies p
                    WHERE p.user_id=%s AND p.model=%s AND p.ends_at>%s AND NOT EXISTS(
                      SELECT 1 FROM retail_model_budget_occurrences o JOIN retail_model_budget_recurrences r ON r.id=o.recurrence_id
                      WHERE o.policy_id=p.id AND r.stops_at IS NOT NULL AND p.effective_from>=r.stops_at)""",
                    (user_id, data.model, start), _Count)
                if conflict and conflict.total:
                    raise HTTPException(409, "An existing budget overlaps the monthly plan")
                identifier: Final = uuid4()
                await connection.execute("""INSERT INTO retail_model_budget_recurrences
                    (id,user_id,model,timezone,starts_at,created_by,updated_by) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (identifier, user_id, data.model, data.timezone, start, actor.user_id, actor.user_id))
                await connection.execute("INSERT INTO retail_model_budget_recurrence_terms VALUES(%s,%s,%s)",
                                         (identifier, start, amount_to_nanos(data.limit_usd)))
                await self._materialize(connection, await self._plan(connection, user_id, identifier), now)
                return await self._audit(connection, await self._read(connection, user_id, identifier), actor, key, action, digest, None)
        except psycopg.errors.ExclusionViolation as exc:
            raise HTTPException(409, "Another budget plan overlaps this account and model") from exc

    async def _plan(self, connection: psycopg.AsyncConnection, user_id: str, identifier: UUID) -> _Plan:
        row: Final = await _one(connection, "SELECT * FROM retail_model_budget_recurrences WHERE id=%s AND user_id=%s FOR UPDATE",
                               (identifier, user_id), _Plan)
        if row is None:
            raise HTTPException(404, "The requested monthly budget is unavailable")
        return row

    async def update(self, user_id: str, identifier: UUID, data: UpdateMonthlyBudget,
                     actor: ControlPlaneActor, key: str) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "monthly_budget.updated"
        digest: Final = operation_digest(action, {"user_id": user_id, "id": str(identifier), **data.model_dump(mode="json")})
        async with self.wallet.diagnostic_transaction() as connection:
            await self._settings(connection)
            await lock_budget_wallet(connection, user_id)
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return replay
            plan: Final = await self._plan(connection, user_id, identifier)
            if plan.version != data.expected_version or plan.stops_at is not None:
                raise HTTPException(409, "The monthly plan changed or has already stopped")
            now: Final = await self._clock(connection)
            effective: Final = max(plan.starts_at, following_month(now, plan.timezone))
            await connection.execute("""INSERT INTO retail_model_budget_recurrence_terms VALUES(%s,%s,%s)
                ON CONFLICT(recurrence_id,effective_from) DO UPDATE SET limit_nanos=EXCLUDED.limit_nanos""",
                (identifier, effective, amount_to_nanos(data.limit_usd)))
            await connection.execute("""UPDATE retail_model_budget_recurrences SET version=version+1,
                updated_at=clock_timestamp(),updated_by=%s,reconcile_after=clock_timestamp() WHERE id=%s""", (actor.user_id, identifier))
            await connection.execute("""UPDATE retail_model_budget_policies p SET limit_nanos=%s,config_version=config_version+1,
                updated_at=clock_timestamp(),updated_by=%s FROM retail_model_budget_occurrences o
                WHERE o.policy_id=p.id AND o.recurrence_id=%s AND p.effective_from>clock_timestamp()
                  AND p.effective_from>=%s""", (amount_to_nanos(data.limit_usd), actor.user_id, identifier, effective))
            await self._materialize(connection, await self._plan(connection, user_id, identifier), now)
            return await self._audit(connection, await self._read(connection, user_id, identifier), actor, key, action, digest, plan.version)

    async def stop(self, user_id: str, identifier: UUID, data: StopMonthlyBudget,
                   actor: ControlPlaneActor, key: str) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "monthly_budget.stopped"
        digest: Final = operation_digest(action, {"user_id": user_id, "id": str(identifier), **data.model_dump(mode="json")})
        async with self.wallet.diagnostic_transaction() as connection:
            await self._settings(connection)
            await lock_budget_wallet(connection, user_id)
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return replay
            plan: Final = await self._plan(connection, user_id, identifier)
            if plan.version != data.expected_version or plan.stops_at is not None:
                raise HTTPException(409, "The monthly plan changed or has already stopped")
            now: Final = await self._clock(connection)
            stop: Final = max(plan.starts_at, following_month(now, plan.timezone))
            await connection.execute("""UPDATE retail_model_budget_recurrences SET stops_at=%s,version=version+1,
                updated_at=clock_timestamp(),updated_by=%s,reconcile_after=clock_timestamp() WHERE id=%s""", (stop, actor.user_id, identifier))
            await connection.execute("""UPDATE retail_model_budget_policies p SET enabled=false,config_version=config_version+1,
                updated_at=clock_timestamp(),updated_by=%s FROM retail_model_budget_occurrences o
                WHERE o.policy_id=p.id AND o.recurrence_id=%s AND p.effective_from>=%s""", (actor.user_id, identifier, stop))
            await self._materialize(connection, await self._plan(connection, user_id, identifier), now)
            return await self._audit(connection, await self._read(connection, user_id, identifier), actor, key, action, digest, plan.version)

    async def _materialize(self, connection: psycopg.AsyncConnection, plan: _Plan, now: datetime) -> None:
        current: Final = month_floor(now, plan.timezone)
        next_start: Final = following_month(now, plan.timezone)
        for start in (current, next_start):
            if start < plan.starts_at or (plan.stops_at is not None and start >= plan.stops_at):
                continue
            identifier: Final = uuid4()
            inserted: Final = await connection.execute("""INSERT INTO retail_model_budget_occurrences
                (recurrence_id,starts_at,ends_at,policy_id) VALUES(%s,%s,%s,%s)
                ON CONFLICT(recurrence_id,starts_at) DO NOTHING RETURNING policy_id""",
                (plan.id, start, following_month(start, plan.timezone), identifier))
            if await inserted.fetchone() is None:
                continue
            term: Final = await _one(connection, """SELECT limit_nanos FROM retail_model_budget_recurrence_terms
                WHERE recurrence_id=%s AND effective_from<=%s ORDER BY effective_from DESC LIMIT 1""", (plan.id, start), _Term)
            if term is None:
                raise HTTPException(503, "The monthly budget terms are unavailable")
            await connection.execute("""INSERT INTO retail_model_budget_policies
                (id,user_id,model,effective_from,ends_at,limit_nanos,enabled,created_by,updated_by)
                VALUES(%s,%s,%s,%s,%s,%s,true,%s,%s)""", (identifier, plan.user_id, plan.model, start,
                    following_month(start, plan.timezone), term.limit_nanos, plan.created_by, plan.updated_by))
        next_due: Final = min(plan.starts_at, next_start) if plan.starts_at > next_start else next_start
        await connection.execute("UPDATE retail_model_budget_recurrences SET reconcile_after=%s WHERE id=%s", (next_due, plan.id))

    async def materialize_batch(self, *, limit: int = 20) -> int:
        if not 1 <= limit <= 20:
            raise ValueError("Monthly renewal batches are limited to twenty plans")
        async with self.wallet.diagnostic_transaction() as connection:
            await self._settings(connection)
            candidates: Final = await _many(connection, """SELECT id,user_id FROM retail_model_budget_recurrences
                WHERE reconcile_after<=clock_timestamp() AND stops_at IS NULL
                ORDER BY reconcile_after,id LIMIT %s""", (limit,), _Candidate)
        return sum([await self._materialize_candidate(candidate) for candidate in candidates])

    async def _materialize_candidate(self, candidate: _Candidate) -> bool:
        try:
            async with self.wallet.diagnostic_transaction() as connection:
                await self._settings(connection)
                locked: Final = await connection.execute("SELECT user_id FROM retail_wallets WHERE user_id=%s FOR UPDATE SKIP LOCKED", (candidate.user_id,))
                if await locked.fetchone() is None:
                    return False
                plan: Final = await self._plan(connection, candidate.user_id, candidate.id)
                now: Final = await self._clock(connection)
                if plan.reconcile_after > now or plan.stops_at is not None:
                    return False
                await self._materialize(connection, plan, now)
        except (psycopg.Error, HTTPException):
            async with self.wallet.diagnostic_transaction() as repair:
                await self._settings(repair)
                await lock_budget_wallet(repair, candidate.user_id)
                await repair.execute("""UPDATE retail_model_budget_recurrences
                    SET reconcile_after=clock_timestamp()+interval '60 seconds' WHERE id=%s""", (candidate.id,))
        return True

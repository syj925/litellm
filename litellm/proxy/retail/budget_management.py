import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .budget_accounting import BudgetPolicyRow, lock_budget_wallet
from .control_plane_access import ControlPlaneActor
from .models import MAX_AMOUNT_NANOS, InvalidAmount, WalletNotFound, amount_to_nanos
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_store import require_admin
from .store import WalletStore, _many, _one

_MONEY: Final = re.compile(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,9})?\Z", re.ASCII)
_NANOS: Final = 1_000_000_000


def money_string(nanos: int) -> str:
    whole, fractional = divmod(nanos, _NANOS)
    return f"{whole}.{fractional:09d}"


def budget_amount(value: str) -> str:
    if not _MONEY.fullmatch(value):
        raise ValueError("Enter a positive USD decimal with at most nine decimal places")
    try:
        nanos: Final = amount_to_nanos(value, allow_zero=False)
    except InvalidAmount as exc:
        raise ValueError("Enter a positive USD decimal within the supported budget limit") from exc
    if nanos > MAX_AMOUNT_NANOS:
        raise ValueError("The budget limit exceeds the supported amount")
    return money_string(nanos)


def exact_model_name(model: str) -> bool:
    return bool(
        model
        and len(model) <= 191
        and model == model.strip()
        and not any(ord(char) < 32 or char in "*?[]" for char in model)
    )


class CreateModelBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=191)
    ends_at: AwareDatetime
    limit_usd: str = Field(min_length=1, max_length=30)
    enabled: StrictBool = True

    @field_validator("model")
    @classmethod
    def exact_model(cls, value: str) -> str:
        if not exact_model_name(value):
            raise ValueError("Select one exact configured public model")
        return value

    @field_validator("limit_usd")
    @classmethod
    def positive_limit(cls, value: str) -> str:
        return budget_amount(value)

    @field_validator("ends_at")
    @classmethod
    def utc_end(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class UpdateModelBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    limit_usd: str | None = Field(default=None, min_length=1, max_length=30)
    enabled: StrictBool | None = None
    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)

    @field_validator("limit_usd")
    @classmethod
    def positive_limit(cls, value: str | None) -> str | None:
        return budget_amount(value) if value is not None else None

    @model_validator(mode="after")
    def editable_fields(self) -> "UpdateModelBudget":
        changed: Final = self.model_fields_set & {"limit_usd", "enabled"}
        if not changed or any(getattr(self, name) is None for name in changed):
            raise ValueError("Supply a budget limit or enabled state; null does not change a budget")
        return self


class ScheduleModelBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)
    ends_at: AwareDatetime
    limit_usd: str = Field(min_length=1, max_length=30)
    enabled: StrictBool = True

    @field_validator("limit_usd")
    @classmethod
    def positive_limit(cls, value: str) -> str:
        return budget_amount(value)

    @field_validator("ends_at")
    @classmethod
    def utc_end(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class ModelBudgetView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    model: str
    effective_from: datetime
    ends_at: datetime
    limit_usd: str
    spent_usd: str
    held_usd: str
    remaining_usd: str
    overrun_usd: str
    enabled: bool
    config_version: int
    accounting_version: int
    accounted_at: datetime | None
    predecessor_id: UUID | None
    successor_id: UUID | None
    recurrence_id: UUID | None = None
    state: Literal["scheduled", "active", "disabled", "expired"]


class AdminModelBudgetView(ModelBudgetView):
    user_id: str
    source: Literal["management_api"]
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str


class _Policy(BudgetPolicyRow):
    observed_at: datetime
    successor_id: UUID | None = None
    recurrence_id: UUID | None = None


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Clock(BaseModel):
    model_config = ConfigDict(frozen=True)
    at: datetime


class _Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    response: Mapping[str, JsonValue]


_POLICY_COLUMNS: Final = "p.*,clock_timestamp() AS observed_at"
_POLICY_SELECT_COLUMNS: Final = (
    _POLICY_COLUMNS
    + ",(SELECT c.id FROM retail_model_budget_policies c WHERE c.predecessor_id=p.id AND c.user_id=p.user_id) AS successor_id"
    + ",(SELECT o.recurrence_id FROM retail_model_budget_occurrences o WHERE o.policy_id=p.id) AS recurrence_id"
)


def budget_view(row: _Policy, *, admin: bool) -> Mapping[str, JsonValue]:
    state: Final = (
        "expired"
        if row.ends_at <= row.observed_at
        else "scheduled"
        if row.effective_from > row.observed_at
        else "active"
        if row.enabled
        else "disabled"
    )
    common: Final = ModelBudgetView(
        id=row.id,
        model=row.model,
        effective_from=row.effective_from,
        ends_at=row.ends_at,
        limit_usd=money_string(row.limit_nanos),
        spent_usd=money_string(row.spent_nanos),
        held_usd=money_string(row.held_nanos),
        remaining_usd=money_string(max(0, row.limit_nanos - row.spent_nanos - row.held_nanos)),
        overrun_usd=money_string(max(0, row.spent_nanos + row.held_nanos - row.limit_nanos)),
        enabled=row.enabled,
        config_version=row.config_version,
        accounting_version=row.accounting_version,
        accounted_at=row.accounted_at,
        predecessor_id=row.predecessor_id,
        successor_id=row.successor_id,
        recurrence_id=row.recurrence_id,
        state=state,
    )
    if not admin:
        return common.model_dump(mode="json")
    return AdminModelBudgetView(
        **common.model_dump(),
        user_id=row.user_id,
        source="management_api",
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    ).model_dump(mode="json")


async def initialize_budget_management_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(
        Path(__file__).with_name("budget_management_schema.sql").read_text(encoding="utf-8"),
        prepare=False,
    )


def configured_budget_models() -> tuple[str, ...]:
    from .api import _configured_model_names

    return tuple(name for name in _configured_model_names() if exact_model_name(name))


def validate_user_id(user_id: str) -> None:
    if not user_id or len(user_id) > 191 or any(ord(char) < 32 for char in user_id):
        raise HTTPException(422, "Enter a valid account identifier")


def pagination(q: str, limit: int, offset: int) -> None:
    if len(q) > 100 or any(ord(char) < 32 for char in q) or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise HTTPException(422, "Invalid budget pagination or search")


class BudgetManagementStore:
    def __init__(self, store: WalletStore, *, models: Callable[[], tuple[str, ...]] = configured_budget_models) -> None:
        self._store = store
        self._models = models

    async def _wallet_exists(self, connection: psycopg.AsyncConnection, user_id: str) -> None:
        row: Final = await _one(
            connection, "SELECT count(*) AS total FROM retail_wallets WHERE user_id=%s", (user_id,), _Count
        )
        if row is None or row.total != 1:
            raise WalletNotFound("Wallet is not enabled for this user")

    async def models(
        self,
        user_id: str,
        actor: ControlPlaneActor,
        *,
        q: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        pagination(q, limit, offset)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await self._wallet_exists(connection, user_id)
        selected: Final = tuple(
            name for name in self._models() if exact_model_name(name) and q.casefold() in name.casefold()
        )
        return {
            "items": [{"model": name} for name in selected[offset : offset + limit]],
            "total": len(selected),
            "limit": limit,
            "offset": offset,
        }

    async def list(
        self,
        user_id: str,
        actor: ControlPlaneActor,
        *,
        admin: bool,
        q: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> Mapping[str, JsonValue]:
        if admin:
            require_admin(actor)
        elif actor.user_id != user_id:
            raise HTTPException(404, "The requested budget is unavailable")
        validate_user_id(user_id)
        pagination(q, limit, offset)
        pattern: Final = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await self._wallet_exists(connection, user_id)
            count: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_model_budget_policies WHERE user_id=%s AND model ILIKE %s",
                (user_id, pattern),
                _Count,
            )
            rows: Final = await _many(
                connection,
                f"SELECT {_POLICY_SELECT_COLUMNS} FROM retail_model_budget_policies p "
                "WHERE user_id=%s AND model ILIKE %s ORDER BY effective_from DESC,id "
                "LIMIT %s OFFSET %s",
                (user_id, pattern, limit, offset),
                _Policy,
            )
        return {
            "items": [dict(budget_view(row, admin=admin)) for row in rows],
            "total": count.total if count else 0,
            "limit": limit,
            "offset": offset,
        }

    async def _replay(
        self,
        connection: psycopg.AsyncConnection,
        actor: ControlPlaneActor,
        key: str,
        action: str,
        digest: str,
    ) -> Mapping[str, JsonValue] | None:
        previous: Final = await replay_operation(connection, actor.user_id, key, action, digest)
        if previous is None:
            return None
        row: Final = await _one(
            connection,
            "SELECT response FROM retail_model_budget_management_receipts WHERE actor_id=%s AND idempotency_key=%s",
            (actor.user_id, key),
            _Receipt,
        )
        if row is None:
            raise HTTPException(503, "The budget operation receipt is unavailable")
        return row.response

    async def _event(
        self,
        connection: psycopg.AsyncConnection,
        actor: ControlPlaneActor,
        key: str,
        action: str,
        digest: str,
        row: _Policy,
        previous_version: int | None,
        changes: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        response: Final = budget_view(row, admin=True)
        await connection.execute(
            "INSERT INTO retail_model_budget_management_receipts(actor_id,idempotency_key,response) VALUES(%s,%s,%s)",
            (actor.user_id, key, Jsonb(dict(response))),
        )
        await append_operation_event(
            connection,
            actor_id=actor.user_id,
            action=action,
            resource_type="model_budget",
            resource_id=str(row.id),
            organization_id=None,
            version_before=previous_version,
            version_after=row.config_version,
            changes=changes,
            idempotency_key=key,
            request_digest=digest,
            response={"id": str(row.id), "config_version": row.config_version},
        )
        return response

    async def create(
        self,
        user_id: str,
        data: CreateModelBudget,
        actor: ControlPlaneActor,
        key: str,
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "model_budget.created"
        digest: Final = operation_digest(action, {"user_id": user_id, **data.model_dump(mode="json")})
        try:
            async with self._store.diagnostic_transaction() as connection:
                await connection.execute("SET LOCAL statement_timeout='3000ms'")
                await connection.execute("SET LOCAL lock_timeout='1000ms'")
                await lock_budget_wallet(connection, user_id)
                replay: Final = await self._replay(connection, actor, key, action, digest)
                if replay is not None:
                    return replay
                if data.model not in self._models() or not exact_model_name(data.model):
                    raise HTTPException(422, "Select one current configured public model")
                clock: Final = await _one(connection, "SELECT clock_timestamp() AS at", (), _Clock)
                if clock is None:
                    raise HTTPException(503, "The budget clock is unavailable")
                if not clock.at < data.ends_at <= clock.at + timedelta(days=366):
                    raise HTTPException(422, "Budget end must be in the future and within 366 days")
                policy: Final = await _one(
                    connection,
                    "INSERT INTO retail_model_budget_policies AS p "
                    "(id,user_id,model,ends_at,limit_nanos,enabled,created_by,updated_by) "
                    f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_POLICY_COLUMNS}",
                    (
                        uuid4(),
                        user_id,
                        data.model,
                        data.ends_at,
                        amount_to_nanos(data.limit_usd),
                        data.enabled,
                        actor.user_id,
                        actor.user_id,
                    ),
                    _Policy,
                )
                if policy is None:
                    raise HTTPException(503, "The budget could not be created")
                return await self._event(
                    connection,
                    actor,
                    key,
                    action,
                    digest,
                    policy,
                    None,
                    {
                        "user_id": user_id,
                        "model": policy.model,
                        "limit_usd": data.limit_usd,
                        "enabled": policy.enabled,
                        "start": policy.effective_from.isoformat(),
                        "end": policy.ends_at.isoformat(),
                    },
                )
        except psycopg.errors.ExclusionViolation as exc:
            raise HTTPException(409, "Another budget window overlaps this account and model") from exc
        except psycopg.errors.CheckViolation as exc:
            raise HTTPException(422, "The budget amount or window is invalid") from exc

    async def update(
        self,
        user_id: str,
        budget_id: UUID,
        data: UpdateModelBudget,
        actor: ControlPlaneActor,
        key: str,
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "model_budget.updated"
        payload: Final = data.model_dump(mode="json", exclude_unset=True)
        digest: Final = operation_digest(action, {"user_id": user_id, "id": str(budget_id), **payload})
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await connection.execute("SET LOCAL lock_timeout='1000ms'")
            await lock_budget_wallet(connection, user_id)
            previous: Final = await _one(
                connection,
                f"SELECT {_POLICY_SELECT_COLUMNS} FROM retail_model_budget_policies p "
                "WHERE id=%s AND user_id=%s FOR UPDATE OF p",
                (budget_id, user_id),
                _Policy,
            )
            if previous is None:
                raise HTTPException(404, "The requested budget is unavailable")
            monthly: Final = await _one(connection, "SELECT count(*) AS total FROM retail_model_budget_occurrences WHERE policy_id=%s", (budget_id,), _Count)
            if monthly and monthly.total:
                raise HTTPException(409, "Change this monthly window through its renewal plan")
            replay: Final = await self._replay(connection, actor, key, action, digest)
            if replay is not None:
                return replay
            if previous.ends_at <= previous.observed_at:
                raise HTTPException(409, "This budget window has ended and cannot be edited")
            if previous.config_version != data.expected_version:
                raise HTTPException(409, "The budget changed. Refresh before submitting again")
            changed: Final = await _one(
                connection,
                "UPDATE retail_model_budget_policies AS p SET "
                "limit_nanos=COALESCE(%s,limit_nanos),enabled=COALESCE(%s,enabled),"
                "config_version=config_version+1,updated_at=clock_timestamp(),updated_by=%s "
                "WHERE id=%s AND user_id=%s AND config_version=%s AND ends_at>clock_timestamp() "
                f"RETURNING {_POLICY_COLUMNS}",
                (
                    amount_to_nanos(data.limit_usd) if data.limit_usd is not None else None,
                    data.enabled,
                    actor.user_id,
                    budget_id,
                    user_id,
                    data.expected_version,
                ),
                _Policy,
            )
            if changed is None:
                raise HTTPException(409, "The budget changed. Refresh before submitting again")
            changes: Final = {name: value for name, value in payload.items() if name in {"limit_usd", "enabled"}}
            return await self._event(
                connection,
                actor,
                key,
                action,
                digest,
                changed.model_copy(update={"successor_id": previous.successor_id}),
                previous.config_version,
                changes,
            )

    async def schedule(
        self,
        user_id: str,
        budget_id: UUID,
        data: ScheduleModelBudget,
        actor: ControlPlaneActor,
        key: str,
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "model_budget.scheduled"
        digest: Final = operation_digest(
            action, {"user_id": user_id, "id": str(budget_id), **data.model_dump(mode="json")}
        )
        try:
            async with self._store.diagnostic_transaction() as connection:
                await connection.execute("SET LOCAL statement_timeout='3000ms'")
                await connection.execute("SET LOCAL lock_timeout='1000ms'")
                await lock_budget_wallet(connection, user_id)
                previous: Final = await _one(
                    connection,
                    f"SELECT {_POLICY_SELECT_COLUMNS} FROM retail_model_budget_policies p "
                    "WHERE id=%s AND user_id=%s FOR UPDATE OF p",
                    (budget_id, user_id),
                    _Policy,
                )
                if previous is None:
                    raise HTTPException(404, "The requested budget is unavailable")
                monthly: Final = await _one(connection, "SELECT count(*) AS total FROM retail_model_budget_occurrences WHERE policy_id=%s", (budget_id,), _Count)
                if monthly and monthly.total:
                    raise HTTPException(409, "Change this monthly window through its renewal plan")
                replay: Final = await self._replay(connection, actor, key, action, digest)
                if replay is not None:
                    return replay
                clock: Final = await _one(connection, "SELECT clock_timestamp() AS at", (), _Clock)
                if clock is None:
                    raise HTTPException(503, "The budget clock is unavailable")
                if not previous.effective_from <= clock.at < previous.ends_at:
                    raise HTTPException(409, "Only a current budget window can have a successor scheduled")
                if previous.config_version != data.expected_version:
                    raise HTTPException(409, "The budget changed. Refresh before submitting again")
                if previous.successor_id is not None:
                    raise HTTPException(409, "This budget window already has a successor")
                if previous.model not in self._models() or not exact_model_name(previous.model):
                    raise HTTPException(422, "Select one current configured public model")
                if not previous.ends_at < data.ends_at <= previous.ends_at + timedelta(days=366):
                    raise HTTPException(422, "The successor must end after its start and within 366 days")
                policy: Final = await _one(
                    connection,
                    "INSERT INTO retail_model_budget_policies AS p "
                    "(id,user_id,model,predecessor_id,ends_at,limit_nanos,enabled,created_by,updated_by) "
                    f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_POLICY_COLUMNS}",
                    (
                        uuid4(),
                        user_id,
                        previous.model,
                        previous.id,
                        data.ends_at,
                        amount_to_nanos(data.limit_usd),
                        data.enabled,
                        actor.user_id,
                        actor.user_id,
                    ),
                    _Policy,
                )
                if policy is None:
                    raise HTTPException(503, "The successor budget could not be scheduled")
                return await self._event(
                    connection,
                    actor,
                    key,
                    action,
                    digest,
                    policy,
                    None,
                    {
                        "user_id": user_id,
                        "model": policy.model,
                        "predecessor_id": str(previous.id),
                        "limit_usd": data.limit_usd,
                        "enabled": policy.enabled,
                        "start": policy.effective_from.isoformat(),
                        "end": policy.ends_at.isoformat(),
                    },
                )
        except (psycopg.errors.ExclusionViolation, psycopg.errors.UniqueViolation) as exc:
            raise HTTPException(409, "A successor or overlapping budget window already exists") from exc
        except psycopg.errors.CheckViolation as exc:
            raise HTTPException(409, "The budget window is no longer eligible for this successor") from exc

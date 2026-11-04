from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, TypeAlias
from zoneinfo import ZoneInfo

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, field_validator

from .budget_accounting import lock_budget_wallet
from .budget_management import money_string, pagination, validate_user_id
from .control_plane_access import ControlPlaneActor
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_consumption import ORGANIZATION_COHORT_SQL
from .organization_store import require_admin
from .store import WalletStore, _many, _one

AssignmentState: TypeAlias = Literal["unassigned", "active", "organization_disabled", "membership_inactive"]
ConsumptionState: TypeAlias = Literal["settled", "released", "pending", "unknown", "conflict"]
_BEIJING: Final = ZoneInfo("Asia/Shanghai")
_MAX_REQUESTS: Final = 10000


def organization_identifier(value: str) -> str:
    if not value or len(value) > 191 or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("Select a valid organization identifier")
    return value


class BillingOrganizationUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str | None
    expected_version: StrictInt = Field(ge=0, le=9_223_372_036_854_775_806)

    @field_validator("organization_id")
    @classmethod
    def selected_organization(cls, value: str | None) -> str | None:
        return organization_identifier(value) if value is not None else None


class BillingOrganizationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    organization_id: str | None
    organization_name: str | None
    organization_enabled: bool | None
    membership_enabled: bool | None
    version: int
    effective: bool
    state: AssignmentState
    updated_at: datetime | None
    updated_by: str | None


class EligibleBillingOrganization(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    organization_version: int
    membership_version: int


class EligibleBillingOrganizations(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[EligibleBillingOrganization, ...]
    total: int
    limit: int
    offset: int


class OrganizationConsumptionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    user_id: str
    model: str
    captured_at: datetime
    state: ConsumptionState
    settled_usd: str | None
    assignment_version: int
    organization_version: int
    membership_version: int


class OrganizationConsumptionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted_requests: int
    settled_requests: int
    released_requests: int
    pending_requests: int
    unknown_requests: int
    conflict_requests: int
    settled_usd: str
    settled_nanos: str


class OrganizationConsumption(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str
    organization_name: str
    organization_enabled: bool
    axis: Literal["admission"] = "admission"
    snapshot_at: datetime
    start: datetime
    end: datetime
    partial: bool
    live: Literal[True] = True
    max_requests: Literal[10000] = 10000
    total: int
    limit: int
    offset: int
    summary: OrganizationConsumptionSummary
    items: tuple[OrganizationConsumptionItem, ...]


class _Assignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    organization_id: str | None
    organization_name: str | None
    organization_enabled: bool | None
    membership_enabled: bool | None
    version: int
    updated_at: datetime | None
    updated_by: str | None


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


class _Version(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: int


class _Organization(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    enabled: bool
    snapshot_at: datetime


class _ConsumptionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    user_id: str
    model: str
    captured_at: datetime
    state: ConsumptionState
    charge_nanos: int | None
    assignment_version: int
    organization_version: int
    membership_version: int


class _ConsumptionQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    settled: int
    released: int
    pending: int
    unknown: int
    conflict: int
    settled_nanos: int
    inconsistent: int
    items: tuple[_ConsumptionRow, ...]


def assignment_view(row: _Assignment) -> BillingOrganizationView:
    state: Final[AssignmentState] = (
        "unassigned"
        if row.organization_id is None
        else "organization_disabled"
        if not row.organization_enabled
        else "membership_inactive"
        if not row.membership_enabled
        else "active"
    )
    return BillingOrganizationView(**row.model_dump(), effective=state == "active", state=state)


def consumption_window(
    start: datetime | None, end: datetime | None, observed_at: datetime
) -> tuple[datetime, datetime]:
    if (start is None) != (end is None):
        raise HTTPException(422, "Consumption start and end must be supplied together")
    if any(value is not None and value.utcoffset() is None for value in (start, end)):
        raise HTTPException(422, "Consumption times require explicit timezones")
    finish: Final = end.astimezone(timezone.utc) if end is not None else observed_at.astimezone(timezone.utc)
    begin: Final = start.astimezone(timezone.utc) if start is not None else finish - timedelta(days=7)
    if not timedelta(0) < finish - begin <= timedelta(days=31):
        raise HTTPException(422, "Consumption windows must be positive and at most 31 days")
    return begin, finish


def consumption_item(row: _ConsumptionRow) -> OrganizationConsumptionItem:
    return OrganizationConsumptionItem(
        **row.model_dump(exclude={"charge_nanos"}),
        settled_usd=money_string(-row.charge_nanos) if row.state == "settled" and row.charge_nanos is not None else None,
    )


_ASSIGNMENT_QUERY: Final = """SELECT w.user_id,a.organization_id,o.name AS organization_name,
    o.enabled AS organization_enabled,m.enabled AS membership_enabled,COALESCE(a.version,0) AS version,
    a.updated_at,a.updated_by FROM retail_wallets w
    LEFT JOIN retail_billing_organization_assignments a ON a.user_id=w.user_id
    LEFT JOIN retail_organizations o ON o.id=a.organization_id
    LEFT JOIN retail_organization_memberships m ON m.organization_id=a.organization_id AND m.user_id=w.user_id
    WHERE w.user_id=%s"""

_CONSUMPTION_QUERY: Final = ORGANIZATION_COHORT_SQL + """, page AS (
    SELECT request_id,user_id,model,captured_at,state,charge_nanos,
      assignment_version,organization_version,membership_version FROM cohort
    ORDER BY captured_at DESC,request_id DESC LIMIT %s OFFSET %s
  ) SELECT count(*) AS total,count(*) FILTER(WHERE state='settled') AS settled,
    count(*) FILTER(WHERE state='released') AS released,count(*) FILTER(WHERE state='pending') AS pending,
    count(*) FILTER(WHERE state='unknown') AS unknown,count(*) FILTER(WHERE state='conflict') AS conflict,
    COALESCE(sum(-charge_nanos) FILTER(WHERE state='settled'),0) AS settled_nanos,
    count(*) FILTER(WHERE inconsistent) AS inconsistent,
    COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.captured_at DESC,p.request_id DESC) FROM page p),'[]') AS items
    FROM cohort"""


class OrganizationFinanceStore:
    def __init__(self, store: WalletStore) -> None:
        self._store = store

    async def _assignment(self, connection: psycopg.AsyncConnection, user_id: str) -> BillingOrganizationView:
        row: Final = await _one(connection, _ASSIGNMENT_QUERY, (user_id,), _Assignment)
        if row is None:
            raise HTTPException(404, "The requested account wallet is unavailable")
        return assignment_view(row)

    async def get(self, user_id: str, actor: ControlPlaneActor) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            return (await self._assignment(connection, user_id)).model_dump(mode="json")

    async def eligible(
        self, user_id: str, actor: ControlPlaneActor, *, q: str = "", limit: int = 25, offset: int = 0
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        pagination(q, limit, offset)
        pattern: Final = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await self._assignment(connection, user_id)
            condition: Final = """FROM retail_organizations o JOIN retail_organization_memberships m
                ON m.organization_id=o.id WHERE m.user_id=%s AND m.enabled AND o.enabled AND o.name ILIKE %s"""
            count: Final = await _one(connection, "SELECT count(*) AS total " + condition, (user_id, pattern), _Count)
            rows: Final = await _many(
                connection, "SELECT o.id,o.name,o.version AS organization_version,m.version AS membership_version "
                + condition + " ORDER BY o.name,o.id LIMIT %s OFFSET %s",
                (user_id, pattern, limit, offset), EligibleBillingOrganization,
            )
        return EligibleBillingOrganizations(
            items=tuple(rows), total=count.total if count else 0, limit=limit, offset=offset
        ).model_dump(mode="json")

    async def _eligible_choice(self, connection: psycopg.AsyncConnection, organization_id: str, user_id: str) -> None:
        organization: Final = await _one(
            connection, "SELECT version FROM retail_organizations WHERE id=%s AND enabled FOR SHARE",
            (organization_id,), _Version,
        )
        if organization is None:
            raise HTTPException(422, "Select an enabled organization with an active approved membership")
        membership: Final = await _one(
            connection, "SELECT version FROM retail_organization_memberships WHERE organization_id=%s "
            "AND user_id=%s AND enabled FOR SHARE", (organization_id, user_id), _Version,
        )
        if membership is None:
            raise HTTPException(422, "Select an enabled organization with an active approved membership")

    async def update(
        self, user_id: str, data: BillingOrganizationUpdate, actor: ControlPlaneActor, key: str
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        validate_user_id(user_id)
        action: Final = "billing_organization.updated"
        digest: Final = operation_digest(action, {"user_id": user_id, **data.model_dump(mode="json")})
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await connection.execute("SET LOCAL lock_timeout='1000ms'")
            await lock_budget_wallet(connection, user_id)
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return BillingOrganizationView.model_validate(replay).model_dump(mode="json")
            previous: Final = await self._assignment(connection, user_id)
            if previous.version != data.expected_version:
                raise HTTPException(409, "The billing organization changed. Reload before submitting again")
            if data.organization_id is not None:
                await self._eligible_choice(connection, data.organization_id, user_id)
            await connection.execute(
                """INSERT INTO retail_billing_organization_assignments
                (user_id,organization_id,created_by,updated_by) VALUES(%s,%s,%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET organization_id=EXCLUDED.organization_id,
                  version=retail_billing_organization_assignments.version+1,updated_by=EXCLUDED.updated_by,
                  updated_at=clock_timestamp()""", (user_id, data.organization_id, actor.user_id, actor.user_id),
            )
            result: Final = await self._assignment(connection, user_id)
            await append_operation_event(
                connection, actor_id=actor.user_id, action=action, resource_type="billing_organization",
                resource_id=user_id, organization_id=None, version_before=previous.version, version_after=result.version,
                changes={"user_id": user_id, "organization_id": data.organization_id},
                idempotency_key=key, request_digest=digest, response=result.model_dump(mode="json"),
            )
            return result.model_dump(mode="json")

    async def consumption(
        self, organization_id: str, actor: ControlPlaneActor, *, start: datetime | None = None,
        end: datetime | None = None, limit: int = 25, offset: int = 0
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        try:
            organization_identifier(organization_id)
        except ValueError as exc:
            raise HTTPException(422, "Enter a valid organization identifier") from exc
        pagination("", limit, offset)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            organization: Final = await _one(
                connection, "SELECT id,name,enabled,clock_timestamp() AS snapshot_at FROM retail_organizations WHERE id=%s",
                (organization_id,), _Organization,
            )
            if organization is None:
                raise HTTPException(404, "The requested organization is unavailable")
            begin, finish = consumption_window(start, end, organization.snapshot_at)
            query: Final = await _one(
                connection, _CONSUMPTION_QUERY, (organization_id, begin, finish, limit, offset), _ConsumptionQuery
            )
        if query is None or query.inconsistent:
            raise HTTPException(503, "Organization consumption cannot currently be verified")
        if query.total > _MAX_REQUESTS:
            raise HTTPException(422, "This window exceeds 10,000 attributed requests. Select a smaller window")
        return OrganizationConsumption(
            organization_id=organization.id, organization_name=organization.name, organization_enabled=organization.enabled,
            snapshot_at=organization.snapshot_at, start=begin, end=finish,
            partial=finish > organization.snapshot_at.astimezone(_BEIJING).replace(hour=0, minute=0, second=0, microsecond=0),
            total=query.total, limit=limit, offset=offset,
            summary=OrganizationConsumptionSummary(
                accepted_requests=query.total, settled_requests=query.settled, released_requests=query.released,
                pending_requests=query.pending, unknown_requests=query.unknown, conflict_requests=query.conflict,
                settled_usd=money_string(query.settled_nanos), settled_nanos=str(query.settled_nanos),
            ),
            items=tuple(consumption_item(row) for row in query.items),
        ).model_dump(mode="json")

from datetime import datetime
from typing import Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from .budget_management import money_string, pagination
from .control_plane_access import ControlPlaneActor
from .finance_grants import require_finance
from .organization_consumption import ORGANIZATION_COHORT_SQL
from .organization_finance_store import ConsumptionState, OrganizationConsumptionSummary, consumption_window
from .store import WalletStore, _one

_BEIJING: Final = ZoneInfo("Asia/Shanghai")


class DelegatedConsumptionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    model: str
    captured_at: datetime
    state: ConsumptionState
    settled_usd: str | None


class DelegatedConsumption(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str
    organization_name: str
    access_revision: UUID
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
    items: tuple[DelegatedConsumptionItem, ...]


class _Organization(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    snapshot_at: datetime


class _ConsumptionRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int
    model: str
    captured_at: datetime
    state: ConsumptionState
    charge_nanos: int | None


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


def consumption_item(row: _ConsumptionRow) -> DelegatedConsumptionItem:
    return DelegatedConsumptionItem(
        **row.model_dump(exclude={"charge_nanos"}),
        settled_usd=money_string(-row.charge_nanos) if row.state == "settled" and row.charge_nanos is not None else None,
    )


_CONSUMPTION_QUERY: Final = ORGANIZATION_COHORT_SQL + """, numbered AS (
    SELECT row_number() OVER(ORDER BY captured_at DESC,request_id DESC) AS sequence,
      model,captured_at,state,charge_nanos FROM cohort
  ), page AS (
    SELECT * FROM numbered ORDER BY sequence LIMIT %s OFFSET %s
  ) SELECT count(*) AS total,count(*) FILTER(WHERE state='settled') AS settled,
    count(*) FILTER(WHERE state='released') AS released,count(*) FILTER(WHERE state='pending') AS pending,
    count(*) FILTER(WHERE state='unknown') AS unknown,count(*) FILTER(WHERE state='conflict') AS conflict,
    COALESCE(sum(-charge_nanos) FILTER(WHERE state='settled'),0) AS settled_nanos,
    count(*) FILTER(WHERE inconsistent) AS inconsistent,
    COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.sequence) FROM page p),'[]') AS items FROM cohort"""


class DelegatedFinanceStore:
    def __init__(self, wallet: WalletStore) -> None:
        self._wallet = wallet

    async def consumption(
        self, organization_id: str, actor: ControlPlaneActor, *, start: datetime | None = None,
        end: datetime | None = None, limit: int = 25, offset: int = 0,
    ) -> DelegatedConsumption:
        if not actor.can_write:
            raise HTTPException(403, "This account cannot access delegated finance")
        pagination("", limit, offset)
        async with self._wallet.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            authority: Final = await require_finance(connection, organization_id, actor.user_id)
            organization: Final = await _one(
                connection, "SELECT id,name,clock_timestamp() AS snapshot_at FROM retail_organizations "
                "WHERE id=%s AND enabled", (organization_id,), _Organization,
            )
            if organization is None:
                raise HTTPException(404, "The requested organization is unavailable")
            begin, finish = consumption_window(start, end, organization.snapshot_at)
            query: Final = await _one(
                connection, _CONSUMPTION_QUERY, (organization_id, begin, finish, limit, offset), _ConsumptionQuery,
            )
        if query is None or query.inconsistent:
            raise HTTPException(503, "Organization consumption cannot currently be verified")
        if query.total > 10000:
            raise HTTPException(422, "This window exceeds 10,000 attributed requests. Select a smaller window")
        return DelegatedConsumption(
            organization_id=organization.id, organization_name=organization.name, access_revision=authority.revision,
            snapshot_at=organization.snapshot_at, start=begin, end=finish,
            partial=finish > organization.snapshot_at.astimezone(_BEIJING).replace(hour=0, minute=0, second=0, microsecond=0),
            total=query.total, limit=limit, offset=offset,
            summary=OrganizationConsumptionSummary(
                accepted_requests=query.total, settled_requests=query.settled, released_requests=query.released,
                pending_requests=query.pending, unknown_requests=query.unknown, conflict_requests=query.conflict,
                settled_usd=money_string(query.settled_nanos), settled_nanos=str(query.settled_nanos),
            ),
            items=tuple(consumption_item(row) for row in query.items),
        )

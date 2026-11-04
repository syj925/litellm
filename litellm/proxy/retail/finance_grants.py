from datetime import datetime
from pathlib import Path
from typing import Final, Literal, TypeAlias
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .budget_management import pagination, validate_user_id
from .control_plane_access import ControlPlaneActor
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_finance_store import organization_identifier
from .organization_store import require_admin
from .store import WalletStore, _many, _one

GrantState: TypeAlias = Literal[
    "unassigned", "revoked", "active", "organization_disabled", "membership_inactive",
    "membership_changed", "organization_changed",
]


class FinanceGrantUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    can_view: StrictBool
    can_export: StrictBool
    expected_version: StrictInt = Field(ge=0, le=9_223_372_036_854_775_806)

    @model_validator(mode="after")
    def export_requires_view(self) -> "FinanceGrantUpdate":
        if self.can_export and not self.can_view:
            raise ValueError("Export permission requires view permission")
        return self


class FinanceGrantView(BaseModel):
    model_config = ConfigDict(frozen=True)
    organization_id: str
    user_id: str
    grant_id: UUID | None
    version: int
    revision: UUID | None
    can_view: bool
    can_export: bool
    effective: bool
    state: GrantState
    updated_by: str | None
    updated_at: datetime | None


class FinanceGrantPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[FinanceGrantView, ...]
    total: int
    limit: int
    offset: int


class FinanceAccess(BaseModel):
    model_config = ConfigDict(frozen=True)
    organization_id: str
    organization_name: str
    can_view: bool
    can_export: bool
    access_revision: UUID | None


class FinanceScope(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    can_view: bool
    can_export: bool
    access_revision: UUID


class FinanceScopePage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[FinanceScope, ...]
    total: int
    limit: int
    offset: int


class FinanceAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)
    grant_id: UUID
    version: int
    revision: UUID
    member_version: int
    org_generation: int
    organization_id: str
    organization_name: str
    user_id: str
    can_export: bool

    def same_epoch(self, other: "FinanceAuthority") -> bool:
        return (
            self.grant_id, self.version, self.revision, self.member_version, self.org_generation,
            self.organization_id, self.user_id,
        ) == (
            other.grant_id, other.version, other.revision, other.member_version, other.org_generation,
            other.organization_id, other.user_id,
        )


class _Grant(BaseModel):
    model_config = ConfigDict(frozen=True)
    organization_id: str
    user_id: str
    grant_id: UUID
    version: int
    revision: UUID
    can_view: bool
    can_export: bool
    granted_member_version: int
    granted_org_generation: int
    updated_by: str
    updated_at: datetime


class _Organization(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    enabled: bool
    generation: int


class _Member(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str
    role: str
    enabled: bool
    version: int


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int


_EFFECTIVE: Final = """o.enabled AND m.enabled AND m.role='admin' AND g.can_view
    AND g.granted_member_version=m.version
    AND g.granted_org_generation=COALESCE(og.generation,0)"""
_AUTH_FROM: Final = """FROM retail_organization_finance_grants g
    JOIN retail_organizations o ON o.id=g.organization_id
    JOIN retail_organization_memberships m ON m.organization_id=g.organization_id AND m.user_id=g.user_id
    LEFT JOIN retail_finance_organization_generations og ON og.organization_id=o.id"""
FINANCE_JOB_AUTHORIZATION_SQL: Final = "EXISTS(SELECT 1 "+_AUTH_FROM+" WHERE "+_EFFECTIVE+"""
    AND g.can_export AND g.organization_id=j.organization_id AND g.user_id=j.creator_id
    AND g.grant_id=j.grant_id AND g.version=j.grant_version AND g.revision=j.grant_revision
    AND m.version=j.grant_member_version AND COALESCE(og.generation,0)=j.grant_org_generation)"""


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(Path(__file__).with_name("finance_grants_schema.sql").read_text(encoding="utf-8"), prepare=False)


async def require_finance(
    connection: psycopg.AsyncConnection, organization_id: str, user_id: str,
    *, export: bool = False, expected: FinanceAuthority | None = None,
) -> FinanceAuthority:
    authority: Final = await _one(
        connection, """SELECT g.grant_id,g.version,g.revision,m.version AS member_version,
        COALESCE(og.generation,0) AS org_generation,o.id AS organization_id,o.name AS organization_name,
        g.user_id,g.can_export """+_AUTH_FROM+" WHERE o.id=%s AND g.user_id=%s AND "+_EFFECTIVE+
        " AND (NOT %s OR g.can_export)", (organization_id, user_id, export), FinanceAuthority,
    )
    if authority is None:
        raise HTTPException(403, "Organization finance permission is required")
    if expected is not None and not authority.same_epoch(expected):
        raise HTTPException(404, "Report not found")
    return authority


def _view(organization: _Organization, member: _Member | None, grant: _Grant | None, user_id: str) -> FinanceGrantView:
    state: Final[GrantState] = (
        "unassigned" if grant is None else "revoked" if not grant.can_view
        else "organization_disabled" if not organization.enabled
        else "membership_inactive" if member is None or not member.enabled or member.role != "admin"
        else "membership_changed" if grant.granted_member_version != member.version
        else "organization_changed" if grant.granted_org_generation != organization.generation else "active"
    )
    return FinanceGrantView(
        organization_id=organization.id, user_id=user_id, grant_id=grant.grant_id if grant else None,
        version=grant.version if grant else 0, revision=grant.revision if grant else None,
        can_view=grant.can_view if grant else False, can_export=grant.can_export if grant else False,
        effective=state == "active", state=state,
        updated_by=grant.updated_by if grant else None, updated_at=grant.updated_at if grant else None,
    )


class FinanceGrantStore:
    def __init__(self, store: WalletStore) -> None:
        self._store = store

    async def _organization(
        self, connection: psycopg.AsyncConnection, organization_id: str, *, lock: bool = False,
    ) -> _Organization:
        if lock:
            await connection.execute("SELECT id FROM retail_organizations WHERE id=%s FOR UPDATE", (organization_id,))
        row: Final = await _one(
            connection, "SELECT o.id,o.name,o.enabled,COALESCE(g.generation,0) AS generation FROM retail_organizations o "
            "LEFT JOIN retail_finance_organization_generations g ON g.organization_id=o.id WHERE o.id=%s",
            (organization_id,), _Organization,
        )
        if row is None:
            raise HTTPException(404, "The requested organization is unavailable")
        return row

    async def _member(
        self, connection: psycopg.AsyncConnection, organization_id: str, user_id: str, *, lock: bool = False,
    ) -> _Member | None:
        return await _one(connection, "SELECT user_id,role,enabled,version FROM retail_organization_memberships "
                          "WHERE organization_id=%s AND user_id=%s"+(" FOR UPDATE" if lock else ""),
                          (organization_id, user_id), _Member)

    async def _grant(
        self, connection: psycopg.AsyncConnection, organization_id: str, user_id: str, *, lock: bool = False,
    ) -> _Grant | None:
        return await _one(connection, "SELECT * FROM retail_organization_finance_grants WHERE organization_id=%s AND user_id=%s"+
                          (" FOR UPDATE" if lock else ""), (organization_id, user_id), _Grant)

    async def get(self, organization_id: str, user_id: str, actor: ControlPlaneActor) -> FinanceGrantView:
        require_admin(actor)
        organization_identifier(organization_id)
        validate_user_id(user_id)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            organization: Final = await self._organization(connection, organization_id)
            member: Final = await self._member(connection, organization_id, user_id)
            grant: Final = await self._grant(connection, organization_id, user_id)
        return _view(organization, member, grant, user_id)

    async def update(
        self, organization_id: str, user_id: str, data: FinanceGrantUpdate, actor: ControlPlaneActor, key: str,
    ) -> FinanceGrantView:
        require_admin(actor)
        organization_identifier(organization_id)
        validate_user_id(user_id)
        action: Final = "finance_grant.updated"
        digest: Final = operation_digest(action, {"organization_id": organization_id, "user_id": user_id,
                                                **data.model_dump(mode="json")})
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await connection.execute("SET LOCAL lock_timeout='1000ms'")
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            organization: Final = await self._organization(connection, organization_id, lock=True)
            member: Final = await self._member(connection, organization_id, user_id, lock=True)
            previous: Final = await self._grant(connection, organization_id, user_id, lock=True)
            if replay is not None:
                saved: Final = FinanceGrantView.model_validate(replay)
                if previous is None or (saved.grant_id, saved.version, saved.revision) != (
                    previous.grant_id, previous.version, previous.revision,
                ):
                    raise HTTPException(409, "The finance authorization changed. Use a new operation key")
                return _view(organization, member, previous, user_id)
            if (previous.version if previous else 0) != data.expected_version:
                raise HTTPException(409, "The finance authorization changed. Reload before submitting again")
            if data.can_view and (not organization.enabled or member is None or not member.enabled or member.role != "admin"):
                raise HTTPException(422, "Select a current enabled administrator of an enabled organization")
            updated: Final = await _one(connection, """INSERT INTO retail_organization_finance_grants
                (organization_id,user_id,grant_id,version,revision,can_view,can_export,
                 granted_member_version,granted_org_generation,created_by,updated_by)
                VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(organization_id,user_id) DO UPDATE SET
                  version=retail_organization_finance_grants.version+1,revision=EXCLUDED.revision,
                  can_view=EXCLUDED.can_view,can_export=EXCLUDED.can_export,
                  granted_member_version=EXCLUDED.granted_member_version,granted_org_generation=EXCLUDED.granted_org_generation,
                  updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp() RETURNING *""",
                (organization_id, user_id, uuid4(), uuid4(), data.can_view, data.can_export,
                 member.version if member else 0, organization.generation, actor.user_id, actor.user_id), _Grant)
            if updated is None:
                raise RuntimeError("The finance authorization could not be saved")
            result: Final = _view(organization, member, updated, user_id)
            await append_operation_event(
                connection, actor_id=actor.user_id, action=action, resource_type="finance_grant",
                resource_id=str(updated.grant_id), organization_id=None,
                version_before=previous.version if previous else 0, version_after=updated.version,
                changes={"organization_id": organization_id, "user_id": user_id,
                         "can_view": data.can_view, "can_export": data.can_export},
                idempotency_key=key, request_digest=digest, response=result.model_dump(mode="json"),
            )
            return result

    async def access(self, organization_id: str, user_id: str) -> FinanceAccess:
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            organization: Final = await self._organization(connection, organization_id)
            member: Final = await self._member(connection, organization_id, user_id)
            if not organization.enabled or member is None or not member.enabled or member.role != "admin":
                raise HTTPException(404, "The requested organization is unavailable")
            grant: Final = await self._grant(connection, organization_id, user_id)
            view: Final = _view(organization, member, grant, user_id)
        return FinanceAccess(organization_id=organization.id, organization_name=organization.name,
                             can_view=view.effective, can_export=view.effective and view.can_export,
                             access_revision=view.revision if view.effective else None)

    async def list(
        self, organization_id: str, actor: ControlPlaneActor, limit: int = 25, offset: int = 0,
    ) -> FinanceGrantPage:
        require_admin(actor)
        pagination("", limit, offset)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            organization: Final = await self._organization(connection, organization_id)
            count: Final = await _one(connection, "SELECT count(*) AS total FROM retail_organization_finance_grants WHERE organization_id=%s",
                                     (organization_id,), _Count)
            rows: Final = await _many(connection, "SELECT * FROM retail_organization_finance_grants WHERE organization_id=%s "
                                     "ORDER BY user_id LIMIT %s OFFSET %s", (organization_id, limit, offset), _Grant)
            members: Final = await _many(connection, "SELECT user_id,role,enabled,version FROM retail_organization_memberships "
                                        "WHERE organization_id=%s AND user_id=ANY(%s)",
                                        (organization_id, [row.user_id for row in rows]), _Member)
        member_map: Final = {member.user_id: member for member in members}
        return FinanceGrantPage(items=tuple(_view(organization, member_map.get(row.user_id), row, row.user_id) for row in rows),
                                total=count.total if count else 0, limit=limit, offset=offset)

    async def scopes(
        self, user_id: str, q: str = "", limit: int = 25, offset: int = 0, *, ids: tuple[str, ...] | None = None,
    ) -> FinanceScopePage:
        pagination(q, limit, offset)
        pattern: Final = "%"+q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")+"%"
        condition: Final = _AUTH_FROM+" WHERE g.user_id=%s AND "+_EFFECTIVE+" AND o.name ILIKE %s AND (%s::text[] IS NULL OR o.id=ANY(%s))"
        selected: Final = list(ids) if ids is not None else None
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            count: Final = await _one(connection, "SELECT count(*) AS total "+condition, (user_id, pattern, selected, selected), _Count)
            rows: Final = await _many(connection, "SELECT o.id,o.name,g.can_view,g.can_export,g.revision AS access_revision "+
                                     condition+" ORDER BY o.name,o.id LIMIT %s OFFSET %s",
                                     (user_id, pattern, selected, selected, limit, offset), FinanceScope)
        return FinanceScopePage(items=tuple(rows), total=count.total if count else 0, limit=limit, offset=offset)

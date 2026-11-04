from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, TypeAlias
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, field_validator

from .control_plane_access import ControlPlaneActor, NativeIdentityReader, verify_target_user
from .control_plane_flags import organization_delegation_enabled
from .operation_events import append_operation_event, list_operation_events, operation_digest, replay_operation
from .store import WalletStore, _many, _one

OrganizationRole: TypeAlias = Literal["admin", "member"]


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    admin_user_id: str = Field(min_length=1, max_length=191)

    @field_validator("name", "admin_user_id")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("A nonempty value without control characters is required")
        return value.strip()


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=1)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return OrganizationCreate.clean_text(value)


class MemberAssociate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=191)
    role: OrganizationRole
    expected_organization_version: StrictInt = Field(ge=1)

    @field_validator("user_id")
    @classmethod
    def clean_id(cls, value: str) -> str:
        return OrganizationCreate.clean_text(value)


class MemberUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: OrganizationRole
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=1)
    expected_organization_version: StrictInt = Field(ge=1)


class OrganizationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class MembershipRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: OrganizationRole
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class _OrganizationMembership(MembershipRow):
    organization_id: str


class _MemberFinanceGrant(MembershipRow):
    has_finance_grant: bool


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(
        "\n".join(Path(__file__).with_name(name).read_text(encoding="utf-8")
                  for name in ("organization_schema.sql", "finance_grants_schema.sql")), prepare=False
    )


def require_admin(actor: ControlPlaneActor) -> None:
    if not actor.is_admin or not actor.can_write:
        raise HTTPException(403, "Proxy administrator permission is required")


def _check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise HTTPException(409, "The record changed. Refresh before submitting again")


def organization_view(
    row: OrganizationRow, actor: ControlPlaneActor, membership: MembershipRow | None = None
) -> Mapping[str, JsonValue]:
    delegated: Final = bool(
        row.enabled
        and membership
        and membership.enabled
        and membership.role == "admin"
        and organization_delegation_enabled()
    )
    return {
        **row.model_dump(mode="json"),
        "member_role": membership.role if membership is not None and membership.enabled else None,
        "can_edit": actor.is_admin and actor.can_write,
        "can_associate_members": actor.is_admin and actor.can_write,
        "can_manage_members": actor.can_write and (actor.is_admin or delegated),
        "can_view_events": actor.is_admin or delegated,
    }


class OrganizationStore:
    def __init__(self, store: WalletStore, *, identity_reader: NativeIdentityReader | None = None) -> None:
        self._store = store
        self._identity_reader = identity_reader

    async def _membership(
        self, connection: psycopg.AsyncConnection, organization_id: str, user_id: str
    ) -> MembershipRow | None:
        return await _one(
            connection,
            """SELECT user_id,role,enabled,version,created_at,updated_at FROM retail_organization_memberships
            WHERE organization_id=%s AND user_id=%s""",
            (organization_id, user_id),
            MembershipRow,
        )

    async def _access(
        self,
        connection: psycopg.AsyncConnection,
        organization_id: str,
        actor: ControlPlaneActor,
        *,
        write: bool = False,
    ) -> tuple[OrganizationRow, MembershipRow | None]:
        row: Final = await _one(
            connection,
            "SELECT * FROM retail_organizations WHERE id=%s" + (" FOR UPDATE" if write else ""),
            (organization_id,),
            OrganizationRow,
        )
        if row is None:
            raise HTTPException(404, "The requested organization does not exist")
        membership: Final = await self._membership(connection, organization_id, actor.user_id)
        if actor.is_admin:
            return row, membership
        if not row.enabled or membership is None or not membership.enabled:
            raise HTTPException(404, "The requested organization does not exist")
        return row, membership

    @staticmethod
    def _require_roster(actor: ControlPlaneActor, membership: MembershipRow | None) -> None:
        if actor.is_admin:
            return
        if not organization_delegation_enabled() or membership is None or membership.role != "admin":
            raise HTTPException(403, "Organization administrator permission is required")

    async def list(
        self, actor: ControlPlaneActor, *, admin: bool = False, q: str = "", limit: int = 25, offset: int = 0
    ) -> Mapping[str, JsonValue]:
        if admin:
            require_admin(actor)
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(q) > 100:
            raise HTTPException(422, "Invalid organization pagination")
        condition: Final = (
            " WHERE name ILIKE %s"
            if admin
            else """ WHERE name ILIKE %s AND enabled=true AND EXISTS
            (SELECT 1 FROM retail_organization_memberships m
             WHERE m.organization_id=retail_organizations.id AND m.user_id=%s AND m.enabled=true)"""
        )
        params: Final = ("%" + q + "%",) if admin else ("%" + q + "%", actor.user_id)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '3000ms'")
            count: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_organizations" + condition, params, _Count
            )
            rows: Final = await _many(
                connection,
                "SELECT * FROM retail_organizations"
                + condition
                + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
                OrganizationRow,
            )
            memberships: Final = await _many(
                connection,
                """SELECT organization_id,user_id,role,enabled,version,created_at,updated_at
                FROM retail_organization_memberships WHERE user_id=%s AND organization_id=ANY(%s)""",
                (actor.user_id, [row.id for row in rows]),
                _OrganizationMembership,
            )
            indexed: Final = {member.organization_id: member for member in memberships}
            items: Final = [organization_view(row, actor, indexed.get(row.id)) for row in rows]
        return {"items": items, "total": count.total if count else 0, "limit": limit, "offset": offset}

    async def get(self, organization_id: str, actor: ControlPlaneActor) -> Mapping[str, JsonValue]:
        async with self._store.diagnostic_transaction() as connection:
            row, membership = await self._access(connection, organization_id, actor)
            return organization_view(row, actor, membership)

    async def create(self, data: OrganizationCreate, actor: ControlPlaneActor, key: str) -> Mapping[str, JsonValue]:
        require_admin(actor)
        await verify_target_user(data.admin_user_id, reader=self._identity_reader)
        action: Final = "organization.created"
        digest: Final = operation_digest(action, data.model_dump(mode="json"))
        async with self._store.diagnostic_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return replay
            organization_id: Final = str(uuid4())
            row: Final = await _one(
                connection,
                "INSERT INTO retail_organizations(id,name) VALUES(%s,%s) RETURNING *",
                (organization_id, data.name),
                OrganizationRow,
            )
            await connection.execute(
                """INSERT INTO retail_organization_memberships(organization_id,user_id,role,approved_by)
                VALUES(%s,%s,'admin',%s)""",
                (organization_id, data.admin_user_id, actor.user_id),
            )
            if row is None:
                raise RuntimeError("Organization creation did not return its identity")
            member: Final = await self._membership(connection, organization_id, actor.user_id)
            response: Final = organization_view(row, actor, member)
            await append_operation_event(
                connection,
                actor_id=actor.user_id,
                action=action,
                resource_type="organization",
                resource_id=organization_id,
                organization_id=organization_id,
                version_before=None,
                version_after=1,
                changes={"name": data.name, "user_id": data.admin_user_id, "role": "admin"},
                idempotency_key=key,
                request_digest=digest,
                response=response,
            )
            return response

    async def update(
        self, organization_id: str, data: OrganizationUpdate, actor: ControlPlaneActor, key: str
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        action: Final = "organization.updated"
        payload: Final = {"organization_id": organization_id, **data.model_dump(mode="json")}
        digest: Final = operation_digest(action, payload)
        async with self._store.diagnostic_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return replay
            current, membership = await self._access(connection, organization_id, actor, write=True)
            _check_version(current.version, data.expected_version)
            row: Final = await _one(
                connection,
                """UPDATE retail_organizations SET name=%s,enabled=%s,version=version+1,updated_at=clock_timestamp()
                WHERE id=%s AND version=%s RETURNING *""",
                (data.name, data.enabled, organization_id, data.expected_version),
                OrganizationRow,
            )
            if row is None:
                raise HTTPException(409, "The record changed. Refresh before submitting again")
            response: Final = organization_view(row, actor, membership)
            await append_operation_event(
                connection,
                actor_id=actor.user_id,
                action=action,
                resource_type="organization",
                resource_id=organization_id,
                organization_id=organization_id,
                version_before=current.version,
                version_after=row.version,
                changes={"name": data.name, "enabled": data.enabled},
                idempotency_key=key,
                request_digest=digest,
                response=response,
            )
            return response

    async def members(
        self, organization_id: str, actor: ControlPlaneActor, *, limit: int = 25, offset: int = 0
    ) -> Mapping[str, JsonValue]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise HTTPException(422, "Invalid member pagination")
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            organization, own = await self._access(connection, organization_id, actor)
            self._require_roster(actor, own)
            count: Final = await _one(
                connection,
                "SELECT count(*) AS total FROM retail_organization_memberships WHERE organization_id=%s",
                (organization_id,),
                _Count,
            )
            projection: Final = (
                ",EXISTS(SELECT 1 FROM retail_organization_finance_grants g "
                "WHERE g.organization_id=m.organization_id AND g.user_id=m.user_id) AS has_finance_grant"
            ) if actor.is_admin else ""
            row_type: Final[type[MembershipRow]] = _MemberFinanceGrant if actor.is_admin else MembershipRow
            rows: Final = await _many(
                connection,
                "SELECT user_id,role,enabled,version,created_at,updated_at"+projection+
                " FROM retail_organization_memberships m WHERE organization_id=%s ORDER BY created_at,user_id LIMIT %s OFFSET %s",
                (organization_id, limit, offset),
                row_type,
            )
            return {
                "items": [row.model_dump(mode="json") for row in rows],
                "total": count.total if count else 0,
                "limit": limit,
                "offset": offset,
                "organization": dict(organization_view(organization, actor, own)),
            }

    async def associate(
        self, organization_id: str, data: MemberAssociate, actor: ControlPlaneActor, key: str
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        await verify_target_user(data.user_id, reader=self._identity_reader)
        action: Final = "organization.member_associated"
        digest: Final = operation_digest(action, {"organization_id": organization_id, **data.model_dump(mode="json")})
        async with self._store.diagnostic_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            if replay is not None:
                return replay
            organization, own = await self._access(connection, organization_id, actor, write=True)
            _check_version(organization.version, data.expected_organization_version)
            existing: Final = await self._membership(connection, organization_id, data.user_id)
            if existing is not None:
                raise HTTPException(409, "This account association already exists. Edit its current state instead")
            member: Final = await _one(
                connection,
                """INSERT INTO retail_organization_memberships(organization_id,user_id,role,approved_by)
                VALUES(%s,%s,%s,%s) RETURNING user_id,role,enabled,version,created_at,updated_at""",
                (organization_id, data.user_id, data.role, actor.user_id),
                MembershipRow,
            )
            return await self._member_result(
                connection,
                organization,
                own,
                member,
                actor,
                key,
                action,
                digest,
                {"user_id": data.user_id, "role": data.role, "enabled": True},
                None,
            )

    async def update_member(
        self, organization_id: str, user_id: str, data: MemberUpdate, actor: ControlPlaneActor, key: str
    ) -> Mapping[str, JsonValue]:
        if not actor.can_write:
            raise HTTPException(403, "This account has read-only access")
        action: Final = "organization.member_updated"
        digest: Final = operation_digest(
            action, {"organization_id": organization_id, "user_id": user_id, **data.model_dump(mode="json")}
        )
        async with self._store.diagnostic_transaction() as connection:
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            organization, own = await self._access(connection, organization_id, actor, write=True)
            self._require_roster(actor, own)
            current: Final = await self._membership(connection, organization_id, user_id)
            if current is None:
                raise HTTPException(404, "The requested member does not exist")
            if not actor.is_admin and (current.role != "member" or data.role != "member"):
                raise HTTPException(403, "Only a proxy administrator may change organization administrators")
            if replay is not None:
                return replay
            _check_version(organization.version, data.expected_organization_version)
            _check_version(current.version, data.expected_version)
            if current.role == "admin" and current.enabled and (not data.enabled or data.role != "admin"):
                remaining: Final = await _one(
                    connection,
                    """SELECT count(*) AS total FROM retail_organization_memberships
                    WHERE organization_id=%s AND role='admin' AND enabled=true AND user_id<>%s""",
                    (organization_id, user_id),
                    _Count,
                )
                if remaining is None or remaining.total == 0:
                    raise HTTPException(409, "An organization must retain an approved administrator")
            updated: Final = await _one(
                connection,
                """UPDATE retail_organization_memberships SET role=%s,enabled=%s,version=version+1,
                updated_at=clock_timestamp() WHERE organization_id=%s AND user_id=%s AND version=%s
                RETURNING user_id,role,enabled,version,created_at,updated_at""",
                (data.role, data.enabled, organization_id, user_id, data.expected_version),
                MembershipRow,
            )
            return await self._member_result(
                connection,
                organization,
                own,
                updated,
                actor,
                key,
                action,
                digest,
                {"user_id": user_id, "role": data.role, "enabled": data.enabled},
                current.version,
            )

    async def _member_result(
        self,
        connection: psycopg.AsyncConnection,
        organization: OrganizationRow,
        own: MembershipRow | None,
        member: MembershipRow | None,
        actor: ControlPlaneActor,
        key: str,
        action: str,
        digest: str,
        changes: Mapping[str, JsonValue],
        previous: int | None,
    ) -> Mapping[str, JsonValue]:
        if member is None:
            raise HTTPException(409, "The record changed. Refresh before submitting again")
        changed: Final = await _one(
            connection,
            """UPDATE retail_organizations SET version=version+1,updated_at=clock_timestamp()
            WHERE id=%s RETURNING *""",
            (organization.id,),
            OrganizationRow,
        )
        if changed is None:
            raise RuntimeError("The organization changed during its transaction")
        effective_own: Final = member if actor.user_id == member.user_id else own
        response: Final = {
            "organization": dict(organization_view(changed, actor, effective_own)),
            "member": member.model_dump(mode="json"),
        }
        await append_operation_event(
            connection,
            actor_id=actor.user_id,
            action=action,
            resource_type="organization_member",
            resource_id=member.user_id,
            organization_id=organization.id,
            version_before=previous,
            version_after=member.version,
            changes=changes,
            idempotency_key=key,
            request_digest=digest,
            response=response,
        )
        return response

    async def events(
        self,
        organization_id: str,
        actor: ControlPlaneActor,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Mapping[str, JsonValue]:
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            _, own = await self._access(connection, organization_id, actor)
            self._require_roster(actor, own)
            if actor_id is not None and not actor.is_admin:
                raise HTTPException(403, "Filtering by actor is available to proxy administrators only")
            return await list_operation_events(
                connection,
                organization_id=organization_id,
                action=action,
                actor_id=actor_id,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )

    async def admin_events(
        self,
        actor: ControlPlaneActor,
        *,
        organization_id: str | None = None,
        action: str | None = None,
        actor_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return await list_operation_events(
                connection,
                organization_id=organization_id,
                action=action,
                actor_id=actor_id,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )

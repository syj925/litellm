from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter

from .native_audit_store import NativeAuditTransaction, append_native_audit
from .scim_config import ScimSetup
from .scim_groups_protocol import (
    GroupList,
    GroupProfile,
    GroupQuery,
    GroupRecord,
    apply_group_patch,
    matches_group_etag,
    parse_group_replace,
)
from .scim_protocol import ScimFailure
from .scim_store import _Abort, _authority, _write_lock

_COLUMNS: Final = """g.id,g.namespace,g.external_id,g.display_name,g.version,g.created_at,g.updated_at,
    coalesce((SELECT array_agg(m.user_resource_id ORDER BY m.user_resource_id)
        FROM retail_scim_group_members m JOIN retail_scim_users u ON u.id=m.user_resource_id
        WHERE m.group_id=g.id AND NOT m.removed AND NOT u.deleted AND u.namespace=g.namespace),'{}'::uuid[]) AS members"""
_RECORDS: Final = TypeAdapter(tuple[GroupRecord, ...])


class _Identifier(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID


class _MemberUser(_Identifier):
    deleted: bool


class _Edge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_resource_id: UUID


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int


_IDENTIFIERS: Final = TypeAdapter(tuple[_Identifier, ...])
_USERS: Final = TypeAdapter(tuple[_MemberUser, ...])
_EDGES: Final = TypeAdapter(tuple[_Edge, ...])
_COUNTS: Final = TypeAdapter(tuple[_Count, ...])


def _ordered(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(sorted(frozenset(values), key=lambda value: value.int))


async def _lock_users(
    writer: NativeAuditTransaction, setup: ScimSetup, identifiers: tuple[UUID, ...], live: tuple[UUID, ...],
) -> None:
    ordered: Final = _ordered(identifiers)
    rows: Final = _USERS.validate_python(await writer.query_raw(
        "SELECT id,deleted FROM retail_scim_users WHERE namespace=$1 AND id=ANY($2::uuid[]) ORDER BY id FOR UPDATE",
        setup.config.namespace, list(map(str, ordered)),
    ))
    live_ids: Final = frozenset(row.id for row in rows if not row.deleted)
    if len(rows) != len(ordered) or not frozenset(live).issubset(live_ids):
        raise _Abort(ScimFailure(400, "invalidValue"))


async def _rotate(writer: NativeAuditTransaction, setup: ScimSetup, identifiers: tuple[UUID, ...]) -> None:
    if identifiers:
        await writer.query_raw(
            "UPDATE retail_scim_users SET credential_revision=gen_random_uuid() WHERE namespace=$1 AND id=ANY($2::uuid[]) RETURNING id",
            setup.config.namespace, list(map(str, _ordered(identifiers))),
        )


async def group_record(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, *, lock: bool = False,
) -> GroupRecord:
    rows: Final = _RECORDS.validate_python(await writer.query_raw(
        f"SELECT {_COLUMNS} FROM retail_scim_groups g WHERE g.namespace=$1 AND g.id=$2::uuid AND NOT g.deleted"
        + (" FOR UPDATE OF g" if lock else ""), setup.config.namespace, str(identifier),
    ))
    if not rows:
        raise _Abort(ScimFailure(404))
    return rows[0]


async def _unique(writer: NativeAuditTransaction, setup: ScimSetup, external_id: str, identifier: UUID) -> None:
    rows: Final = _COUNTS.validate_python(await writer.query_raw(
        "SELECT count(*) AS total FROM retail_scim_groups WHERE namespace=$1 AND external_id=$2 AND id<>$3::uuid AND NOT deleted",
        setup.config.namespace, external_id, str(identifier),
    ))
    if rows[0].total:
        raise _Abort(ScimFailure(409, "uniqueness"))


async def _members(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, members: tuple[UUID, ...],
) -> None:
    settings: Final = await _authority(writer, setup)
    existing: Final = _EDGES.validate_python(await writer.query_raw(
        "SELECT user_resource_id FROM retail_scim_group_members WHERE group_id=$1::uuid", str(identifier),
    ))
    previous: Final = frozenset(row.user_resource_id for row in existing)
    retained: Final = sum(member not in previous for member in members)
    if settings.retained_membership_count + retained > settings.max_memberships:
        raise _Abort(ScimFailure(503))
    await writer.query_raw(
        "UPDATE retail_scim_group_members SET removed=true WHERE group_id=$1::uuid AND NOT(user_resource_id=ANY($2::uuid[])) RETURNING group_id",
        str(identifier), list(map(str, members)),
    )
    if members:
        await writer.query_raw(
            """INSERT INTO retail_scim_group_members(group_id,user_resource_id)
            SELECT $1::uuid,value FROM unnest($2::uuid[]) AS value
            ON CONFLICT(group_id,user_resource_id) DO UPDATE SET removed=false RETURNING group_id""",
            str(identifier), list(map(str, members)),
        )
    if retained:
        await writer.query_raw(
            "UPDATE retail_scim_settings SET retained_membership_count=retained_membership_count+$1 WHERE singleton RETURNING singleton",
            retained,
        )


async def create_group(writer: NativeAuditTransaction, setup: ScimSetup, profile: GroupProfile) -> GroupRecord:
    await _write_lock(writer)
    settings: Final = await _authority(writer, setup)
    identifier: Final = uuid4()
    await _unique(writer, setup, profile.external_id, identifier)
    if settings.retained_group_count >= settings.max_groups:
        raise _Abort(ScimFailure(503))
    await writer.query_raw(
        "INSERT INTO retail_scim_groups(id,namespace,external_id,display_name) VALUES($1::uuid,$2,$3,$4) RETURNING id",
        str(identifier), setup.config.namespace, profile.external_id, profile.display_name,
    )
    await _lock_users(writer, setup, profile.members, profile.members)
    await _members(writer, setup, identifier, profile.members)
    await writer.query_raw(
        "UPDATE retail_scim_groups SET member_count=$2 WHERE id=$1::uuid RETURNING id", str(identifier), len(profile.members),
    )
    await writer.query_raw(
        "UPDATE retail_scim_settings SET retained_group_count=retained_group_count+1 WHERE singleton RETURNING singleton",
    )
    result: Final = await group_record(writer, setup, identifier)
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.group_created")
    return result


async def update_group(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, body: bytes, *,
    replace: bool, if_match: str | None,
) -> GroupRecord:
    await _write_lock(writer)
    await _authority(writer, setup)
    current: Final = await group_record(writer, setup, identifier, lock=True)
    matched: Final = matches_group_etag(if_match, current)
    if isinstance(matched, ScimFailure):
        raise _Abort(matched)
    if not matched:
        raise _Abort(ScimFailure(412))
    change: Final = parse_group_replace(body, current) if replace else apply_group_patch(body, current)
    if isinstance(change, ScimFailure):
        raise _Abort(change)
    if (change.profile.external_id, change.profile.display_name, frozenset(change.profile.members)) == (
        current.external_id, current.display_name, frozenset(current.members),
    ) and not change.removed_members:
        return current
    await _lock_users(writer, setup, (*current.members, *change.profile.members, *change.removed_members), change.profile.members)
    await _members(writer, setup, identifier, change.profile.members)
    await _rotate(writer, setup, change.removed_members)
    await writer.query_raw(
        """UPDATE retail_scim_groups SET display_name=$2,member_count=$3,version=version+1,updated_at=clock_timestamp()
        WHERE id=$1::uuid RETURNING id""", str(identifier), change.profile.display_name, len(change.profile.members),
    )
    result: Final = await group_record(writer, setup, identifier)
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.group_updated")
    return result


async def delete_group(
    writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID, if_match: str | None,
) -> None:
    await _write_lock(writer)
    await _authority(writer, setup)
    current: Final = await group_record(writer, setup, identifier, lock=True)
    matched: Final = matches_group_etag(if_match, current)
    if isinstance(matched, ScimFailure):
        raise _Abort(matched)
    if not matched:
        raise _Abort(ScimFailure(412))
    rows: Final = _EDGES.validate_python(await writer.query_raw(
        "SELECT user_resource_id FROM retail_scim_group_members WHERE group_id=$1::uuid AND NOT removed", str(identifier),
    ))
    members: Final = tuple(row.user_resource_id for row in rows)
    await _lock_users(writer, setup, members, ())
    await _rotate(writer, setup, members)
    await writer.query_raw(
        "UPDATE retail_scim_group_members SET removed=true WHERE group_id=$1::uuid RETURNING group_id", str(identifier),
    )
    await writer.query_raw(
        "UPDATE retail_scim_groups SET deleted=true,member_count=0,version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING id",
        str(identifier),
    )
    await append_native_audit(writer, actor_id="system:scim-directory", action="native_scim.group_deleted")


def _where(query: GroupQuery) -> tuple[str, tuple[object, ...]]:
    match query.filter_field:
        case None:
            return "", ()
        case "id":
            return " AND g.id::text=$2", (query.filter_value,)
        case "external_id":
            return " AND g.external_id=$2", (query.filter_value,)
        case "display_name":
            return " AND g.display_name=$2", (query.filter_value,)


async def list_groups(writer: NativeAuditTransaction, setup: ScimSetup, query: GroupQuery) -> GroupList:
    clause, values = _where(query)
    parameters: Final = (setup.config.namespace, *values)
    count: Final = _COUNTS.validate_python(await writer.query_raw(
        "SELECT count(*) AS total FROM retail_scim_groups g WHERE g.namespace=$1 AND NOT g.deleted" + clause, *parameters,
    ))[0].total
    records: Final = _RECORDS.validate_python(await writer.query_raw(
        "WITH page AS (SELECT g.* FROM retail_scim_groups g WHERE g.namespace=$1 AND NOT g.deleted" + clause
        + f" ORDER BY g.created_at,g.id LIMIT ${len(parameters)+1} OFFSET ${len(parameters)+2}),"
        + "budget AS (SELECT page.*,sum(member_count) OVER(ORDER BY created_at,id) AS response_members FROM page) "
        + f"SELECT {_COLUMNS} FROM budget g WHERE response_members<=1000 ORDER BY g.created_at,g.id",
        *parameters, query.count, min(query.start_index-1, 1000),
    ))
    return GroupList(resources=records, total=count, start_index=query.start_index)


async def detach_user(writer: NativeAuditTransaction, setup: ScimSetup, identifier: UUID) -> int:
    groups: Final = _IDENTIFIERS.validate_python(await writer.query_raw(
        """SELECT g.id FROM retail_scim_groups g JOIN retail_scim_group_members m ON m.group_id=g.id
        WHERE g.namespace=$1 AND NOT g.deleted AND m.user_resource_id=$2::uuid AND NOT m.removed
        ORDER BY g.id FOR UPDATE OF g""", setup.config.namespace, str(identifier),
    ))
    if not groups:
        return 0
    await _lock_users(writer, setup, (identifier,), ())
    await writer.query_raw(
        "UPDATE retail_scim_group_members SET removed=true WHERE user_resource_id=$1::uuid AND group_id=ANY($2::uuid[]) RETURNING group_id",
        str(identifier), [str(group.id) for group in groups],
    )
    await writer.query_raw(
        "UPDATE retail_scim_groups SET member_count=member_count-1,version=version+1,updated_at=clock_timestamp() WHERE id=ANY($1::uuid[]) RETURNING id",
        [str(group.id) for group in groups],
    )
    return len(groups)

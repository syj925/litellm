import re
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from json import dumps
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, field_validator

from .control_plane_access import ControlPlaneActor
from .control_plane_flags import organizations_enabled
from .object_metadata_targets import (
    NativeTarget,
    NativeTargetPage,
    NativeTargetReader,
    ObjectKind,
    native_target_reader,
    read_native_page,
    read_native_target,
    safe_actor_name,
    safe_display_name,
)
from .operation_events import append_operation_event, operation_digest, replay_operation
from .organization_store import require_admin
from .store import WalletStore, _many, _one

_SLUG: Final = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
_UUID_TAG: Final = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_HEX_TAG: Final = re.compile(r"[0-9a-f]{16,}\Z", re.ASCII)
_NUMERIC_TAG: Final = re.compile(r"[0-9]{16,}", re.ASCII)


class MetadataUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tags: tuple[str, ...] = Field(max_length=10)
    expected_version: StrictInt = Field(ge=0)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(tags)) != len(tags):
            raise ValueError("Business labels must be unique")
        if any(
            len(tag) > 40
            or not _SLUG.fullmatch(tag)
            or _UUID_TAG.fullmatch(tag)
            or _HEX_TAG.fullmatch(tag)
            or _NUMERIC_TAG.search(tag)
            for tag in tags
        ):
            raise ValueError("Use 1-40 character lowercase ASCII slugs without identifiers or numeric tokens")
        return tuple(sorted(tags))


class MetadataRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference_id: UUID | None = Field(default=None, exclude=True)
    tags: tuple[str, ...] = ()
    version: int = 0
    source: str | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class _Binding(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: ObjectKind
    native_id: str = Field(repr=False)
    object_created_at: datetime
    scope_fingerprint: str = Field(repr=False)


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int


class _Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    response: Mapping[str, JsonValue]


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(
        Path(__file__).with_name("object_metadata_schema.sql").read_text(encoding="utf-8"), prepare=False
    )


def scope_fingerprint(kind: ObjectKind, target: NativeTarget) -> str:
    if kind != "key":
        return ""
    return sha256(dumps((target.owner_id, target.team_id), separators=(",", ":")).encode("utf-8")).hexdigest()


def scope_note(kind: ObjectKind) -> str:
    match kind:
        case "key":
            return (
                "Keys without a creation time are excluded. Rotation starts new labels. Current ownership must match; "
                "unobserved transfer history is not tracked."
            )
        case "model":
            return "Only database-managed model deployments are listed. Configuration-file models are excluded."
        case "organization":
            return "Retail organizations only. Labels do not change membership, wallet ownership or permissions."
        case "team":
            return "Native teams only. Labels do not change membership, budgets or permissions."


def _check_kind(kind: ObjectKind) -> None:
    if kind == "organization" and not organizations_enabled():
        raise HTTPException(404, "Organization management is not enabled")


def _view(binding: _Binding, target: NativeTarget, metadata: MetadataRow) -> Mapping[str, JsonValue]:
    return {
        "id": str(binding.id),
        "kind": binding.kind,
        "name": safe_display_name(target.name, "Unnamed " + binding.kind),
        "object_created_at": target.created_at.isoformat(),
        "object_created_by": safe_actor_name(target.created_by),
        "object_updated_at": target.updated_at.isoformat() if target.updated_at is not None else None,
        "object_updated_by": safe_actor_name(target.updated_by),
        "metadata": metadata.model_dump(mode="json"),
    }


class ObjectMetadataStore:
    def __init__(self, store: WalletStore, *, target_reader: NativeTargetReader | None = None) -> None:
        self._store = store
        self._target_reader = target_reader

    def _reader(self) -> NativeTargetReader:
        return self._target_reader if self._target_reader is not None else native_target_reader()

    async def _organization(
        self, connection: psycopg.AsyncConnection, native_id: str, *, lock: bool = False
    ) -> NativeTarget | None:
        return await _one(
            connection,
            "SELECT id AS native_id,name,created_at,updated_at FROM retail_organizations WHERE id=%s"
            + (" FOR SHARE" if lock else ""),
            (native_id,),
            NativeTarget,
        )

    async def _current(
        self, connection: psycopg.AsyncConnection, kind: ObjectKind, reference_id: UUID, *, lock: bool = False
    ) -> tuple[_Binding, NativeTarget]:
        binding: Final = await _one(
            connection,
            "SELECT * FROM retail_object_bindings WHERE id=%s AND kind=%s" + (" FOR UPDATE" if lock else ""),
            (reference_id, kind),
            _Binding,
        )
        if binding is None:
            raise HTTPException(404, "The requested object is unavailable")
        target: Final = (
            await self._organization(connection, binding.native_id, lock=lock)
            if kind == "organization"
            else await read_native_target(self._reader(), kind, binding.native_id)
        )
        if (
            target is None
            or target.created_at != binding.object_created_at
            or scope_fingerprint(kind, target) != binding.scope_fingerprint
        ):
            raise HTTPException(404, "The object was removed, replaced or transferred. Refresh the target list")
        return binding, target

    async def _metadata(self, connection: psycopg.AsyncConnection, reference_id: UUID) -> MetadataRow:
        row: Final = await _one(
            connection, "SELECT * FROM retail_object_metadata WHERE reference_id=%s", (reference_id,), MetadataRow
        )
        return row if row is not None else MetadataRow()

    async def targets(
        self, kind: ObjectKind, actor: ControlPlaneActor, *, q: str = "", limit: int = 25, offset: int = 0
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        _check_kind(kind)
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000 or len(q) > 100 or any(ord(char) < 32 for char in q):
            raise HTTPException(422, "Invalid object pagination or search")
        if kind != "organization":
            page: Final = await read_native_page(self._reader(), kind, q, limit, offset)
            async with self._store.diagnostic_transaction() as connection:
                await connection.execute("SET LOCAL statement_timeout='3000ms'")
                return await self._page(connection, kind, page, limit, offset)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            pattern: Final = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            count: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_organizations WHERE name ILIKE %s", (pattern,), _Count
            )
            rows: Final = await _many(
                connection,
                "SELECT id AS native_id,name,created_at,updated_at FROM retail_organizations WHERE name ILIKE %s"
                " ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                (pattern, limit, offset),
                NativeTarget,
            )
            return await self._page(
                connection, kind, NativeTargetPage(tuple(rows), count.total if count else 0), limit, offset
            )

    async def _page(
        self, connection: psycopg.AsyncConnection, kind: ObjectKind, page: NativeTargetPage, limit: int, offset: int
    ) -> Mapping[str, JsonValue]:
        if not page.items:
            return {"items": [], "total": page.total, "limit": limit, "offset": offset, "scope_note": scope_note(kind)}
        identities: Final = [target.native_id for target in page.items]
        stamps: Final = [target.created_at for target in page.items]
        fingerprints: Final = [scope_fingerprint(kind, target) for target in page.items]
        await connection.execute(
            """INSERT INTO retail_object_bindings(id,kind,native_id,object_created_at,scope_fingerprint)
            SELECT unnest(%s::uuid[]),%s,unnest(%s::text[]),unnest(%s::timestamptz[]),unnest(%s::text[])
            ON CONFLICT(kind,native_id,object_created_at,scope_fingerprint) DO NOTHING""",
            ([uuid4() for _ in page.items], kind, identities, stamps, fingerprints),
        )
        bindings: Final = await _many(
            connection,
            """SELECT b.* FROM retail_object_bindings b JOIN
            unnest(%s::text[],%s::timestamptz[],%s::text[]) AS t(native_id,stamp,fingerprint)
            ON b.native_id=t.native_id AND b.object_created_at=t.stamp AND b.scope_fingerprint=t.fingerprint
            WHERE b.kind=%s""",
            (identities, stamps, fingerprints, kind),
            _Binding,
        )
        indexed: Final = {(row.native_id, row.object_created_at, row.scope_fingerprint): row for row in bindings}
        metadata: Final = await _many(
            connection,
            "SELECT * FROM retail_object_metadata WHERE reference_id=ANY(%s::uuid[])",
            ([row.id for row in bindings],),
            MetadataRow,
        )
        by_reference: Final = {row.reference_id: row for row in metadata}
        ordered: Final = tuple(
            indexed[(target.native_id, target.created_at, scope_fingerprint(kind, target))] for target in page.items
        )
        items: Final = [
            _view(binding, target, by_reference.get(binding.id, MetadataRow()))
            for binding, target in zip(ordered, page.items, strict=True)
        ]
        return {"items": items, "total": page.total, "limit": limit, "offset": offset, "scope_note": scope_note(kind)}

    async def get(self, kind: ObjectKind, reference_id: UUID, actor: ControlPlaneActor) -> Mapping[str, JsonValue]:
        require_admin(actor)
        _check_kind(kind)
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            binding, target = await self._current(connection, kind, reference_id)
            return _view(binding, target, await self._metadata(connection, reference_id))

    async def update(
        self, kind: ObjectKind, reference_id: UUID, data: MetadataUpdate, actor: ControlPlaneActor, key: str
    ) -> Mapping[str, JsonValue]:
        require_admin(actor)
        _check_kind(kind)
        action: Final = "object_metadata.updated"
        digest: Final = operation_digest(
            action, {"kind": kind, "id": str(reference_id), **data.model_dump(mode="json")}
        )
        async with self._store.diagnostic_transaction() as connection:
            await connection.execute("SET LOCAL statement_timeout='3000ms'")
            await connection.execute("SET LOCAL lock_timeout='1000ms'")
            replay: Final = await replay_operation(connection, actor.user_id, key, action, digest)
            binding, target = await self._current(connection, kind, reference_id, lock=True)
            if replay is not None:
                receipt: Final = await _one(
                    connection,
                    "SELECT response FROM retail_object_metadata_receipts WHERE actor_id=%s AND idempotency_key=%s",
                    (actor.user_id, key),
                    _Receipt,
                )
                if receipt is None:
                    raise HTTPException(503, "The operation receipt is unavailable")
                return receipt.response
            previous: Final = await self._metadata(connection, reference_id)
            if previous.version != data.expected_version:
                raise HTTPException(409, "The labels changed. Refresh before submitting again")
            updated: Final = await _one(
                connection,
                """INSERT INTO retail_object_metadata(reference_id,tags,version,source,created_by,updated_by)
                VALUES(%s,%s,1,'management_api',%s,%s) ON CONFLICT(reference_id) DO UPDATE SET
                tags=EXCLUDED.tags,version=retail_object_metadata.version+1,updated_at=clock_timestamp(),
                updated_by=EXCLUDED.updated_by RETURNING *""",
                (reference_id, list(data.tags), actor.user_id, actor.user_id),
                MetadataRow,
            )
            if updated is None:
                raise HTTPException(503, "Management storage is temporarily unavailable")
            response: Final = _view(binding, target, updated)
            await connection.execute(
                "INSERT INTO retail_object_metadata_receipts(actor_id,idempotency_key,response) VALUES(%s,%s,%s)",
                (actor.user_id, key, Jsonb(dict(response))),
            )
            await append_operation_event(
                connection,
                actor_id=actor.user_id,
                action=action,
                resource_type="object_metadata",
                resource_id=str(reference_id),
                organization_id=None,
                version_before=previous.version,
                version_after=updated.version,
                changes={"kind": kind, "tag_count": len(data.tags)},
                idempotency_key=key,
                request_digest=digest,
                response={"id": str(reference_id), "kind": kind, "version": updated.version},
            )
            return response

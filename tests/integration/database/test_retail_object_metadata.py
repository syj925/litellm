import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import JsonValue, TypeAdapter

from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.models import IdempotencyConflict
from litellm.proxy.retail.object_metadata_store import MetadataUpdate, ObjectMetadataStore
from litellm.proxy.retail.object_metadata_store import initialize_schema as initialize_metadata
from litellm.proxy.retail.object_metadata_targets import ObjectKind, PrismaTargetReader
from litellm.proxy.retail.operation_events import initialize_schema as initialize_events
from litellm.proxy.retail.organization_store import initialize_schema as initialize_organizations
from litellm.proxy.retail.store import WalletStore

ADMIN: Final = ControlPlaneActor("proxy-admin", "proxy_admin", is_admin=True, can_write=True)
NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ITEMS: Final = TypeAdapter(tuple[Mapping[str, JsonValue], ...])
_JSON: Final = TypeAdapter(Mapping[str, JsonValue])


@dataclass(frozen=True, slots=True)
class NativeTransaction:
    connection: psycopg.AsyncConnection

    async def execute_raw(self, query: str, *args: object) -> object:
        return await self.connection.execute(query, args)

    async def query_raw(self, query: str, *args: object) -> object:
        return await (await self.connection.execute(re.sub(r"\$[0-9]+", "%s", query), args)).fetchall()


@dataclass(frozen=True, slots=True)
class NativeClient:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[NativeTransaction]:
        async with (
            await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection,
            connection.transaction(),
        ):
            yield NativeTransaction(connection)


@dataclass(frozen=True, slots=True)
class MetadataRuntime:
    store: WalletStore
    native_url: str
    metadata: ObjectMetadataStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MetadataRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated object-metadata test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_OBJECT_METADATA_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_labels_" + uuid4().hex, "native_labels_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    retail_url: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    store: Final = WalletStore(retail_url, max_connections=8, diagnostic_connections=3)
    metadata: Final = ObjectMetadataStore(store, target_reader=PrismaTargetReader(NativeClient(native_url)))
    try:
        async with await psycopg.AsyncConnection.connect(retail_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with await psycopg.AsyncConnection.connect(native_url) as connection:
            await connection.execute(
                """CREATE TABLE "LiteLLM_VerificationToken"(token TEXT PRIMARY KEY,key_alias TEXT,key_name TEXT,
                created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ,user_id TEXT,team_id TEXT,metadata JSONB DEFAULT '{}')"""
            )
            await connection.execute(
                """CREATE TABLE "LiteLLM_TeamTable"(team_id TEXT PRIMARY KEY,team_alias TEXT,
                created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ,metadata JSONB DEFAULT '{}')"""
            )
            await connection.execute(
                """CREATE TABLE "LiteLLM_ProxyModelTable"(model_id TEXT PRIMARY KEY,model_name TEXT,
                created_at TIMESTAMPTZ,created_by TEXT,updated_at TIMESTAMPTZ,updated_by TEXT,litellm_params JSONB)"""
            )
            await connection.execute(
                """INSERT INTO "LiteLLM_VerificationToken"
                (token,key_alias,key_name,created_at,updated_at,user_id,team_id,metadata)
                VALUES('private-reference-a','Production','masked-name',%s,%s,'owner-a','team-a','{"native":"untouched"}'),
                ('private-reference-legacy','Legacy','masked-name',NULL,NULL,'owner-a',NULL,'{}')""",
                (NOW, NOW),
            )
            await connection.execute(
                """INSERT INTO "LiteLLM_TeamTable"(team_id,team_alias,created_at,updated_at)
                VALUES('team-a','Example team',%s,%s)""",
                (NOW, NOW),
            )
            await connection.execute(
                """INSERT INTO "LiteLLM_ProxyModelTable"
                VALUES('model-a','Example model',%s,'model-creator',%s,'model-editor','{"api_key":"private-param"}')""",
                (NOW, NOW),
            )
        await store.initialize()
        async with store.diagnostic_transaction() as connection:
            await initialize_events(connection)
            await initialize_organizations(connection)
            await initialize_metadata(connection)
            await connection.execute(
                "INSERT INTO retail_organizations(id,name) VALUES('organization-a','Example organization')"
            )
        yield MetadataRuntime(store, native_url, metadata)
    finally:
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _target(runtime: MetadataRuntime, kind: ObjectKind = "key") -> Mapping[str, JsonValue]:
    page: Final = await runtime.metadata.targets(kind, ADMIN)
    items: Final = _ITEMS.validate_python(page["items"])
    assert len(items) == 1 and page["total"] == 1
    return items[0]


def _reference(target: Mapping[str, JsonValue]) -> UUID:
    value: Final = target["id"]
    assert isinstance(value, str)
    return UUID(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("key", "team", "organization", "model"))
async def test_four_target_kinds_have_opaque_refs_and_separate_metadata_provenance(
    runtime: MetadataRuntime, kind: ObjectKind
) -> None:
    target: Final = await _target(runtime, kind)
    assert target["metadata"] == {
        "tags": [],
        "version": 0,
        "source": None,
        "created_at": None,
        "created_by": None,
        "updated_at": None,
        "updated_by": None,
    }
    result: Final = await runtime.metadata.update(
        kind, _reference(target), MetadataUpdate(tags=("production", "owner-a"), expected_version=0), ADMIN, "save"
    )
    saved: Final = _JSON.validate_python(result["metadata"])
    assert saved["tags"] == ["owner-a", "production"] and saved["version"] == 1
    assert saved["source"] == "management_api" and saved["created_by"] == ADMIN.user_id
    assert saved["updated_by"] == ADMIN.user_id and saved["created_at"] == saved["updated_at"]
    assert result["object_created_at"] == target["object_created_at"]
    assert (await runtime.metadata.get(kind, _reference(target), ADMIN)) == result
    assert "private-reference" not in str(result) and "private-param" not in str(result)
    assert "native_id" not in result and "token" not in result


@pytest.mark.asyncio
async def test_concurrent_idempotent_saves_emit_one_event_without_label_contents(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    data: Final = MetadataUpdate(tags=("private-business-label",), expected_version=0)
    responses: Final = await asyncio.gather(
        *(runtime.metadata.update("key", _reference(target), data, ADMIN, "retry") for _ in range(6))
    )
    assert all(response == responses[0] for response in responses)
    async with runtime.store.diagnostic_transaction() as connection:
        rows: Final = await (
            await connection.execute("SELECT * FROM retail_operation_events WHERE action='object_metadata.updated'")
        ).fetchall()
        receipts: Final = await (
            await connection.execute("SELECT count(*) AS n FROM retail_object_metadata_receipts")
        ).fetchone()
    assert len(rows) == 1 and receipts == {"n": 1}
    assert rows[0]["organization_id"] is None and rows[0]["changes"] == {"kind": "key", "tag_count": 1}
    assert rows[0]["response"] == {"id": str(_reference(target)), "kind": "key", "version": 1}
    assert "private-business-label" not in str(rows) and "private-reference-a" not in str(rows)
    with pytest.raises(IdempotencyConflict):
        await runtime.metadata.update(
            "key", _reference(target), MetadataUpdate(tags=("changed",), expected_version=0), ADMIN, "retry"
        )


@pytest.mark.asyncio
async def test_version_cas_has_one_winner_and_clearing_preserves_creation_provenance(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    results: Final = await asyncio.gather(
        runtime.metadata.update(
            "key", _reference(target), MetadataUpdate(tags=("first",), expected_version=0), ADMIN, "first"
        ),
        runtime.metadata.update(
            "key", _reference(target), MetadataUpdate(tags=("second",), expected_version=0), ADMIN, "second"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results) == 1
    current: Final = await runtime.metadata.get("key", _reference(target), ADMIN)
    metadata: Final = _JSON.validate_python(current["metadata"])
    cleared: Final = await runtime.metadata.update(
        "key", _reference(target), MetadataUpdate(tags=(), expected_version=1), ADMIN, "clear"
    )
    final: Final = _JSON.validate_python(cleared["metadata"])
    assert final["tags"] == [] and final["version"] == 2
    assert final["created_at"] == metadata["created_at"] and final["created_by"] == metadata["created_by"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("rotate", "recreate", "transfer", "team-transfer"))
async def test_key_lifecycle_changes_cannot_reuse_labels_or_replay_old_reference(
    runtime: MetadataRuntime, change: str
) -> None:
    target: Final = await _target(runtime)
    data: Final = MetadataUpdate(tags=("original",), expected_version=0)
    await runtime.metadata.update("key", _reference(target), data, ADMIN, "original")
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        if change == "rotate":
            await connection.execute(
                'UPDATE "LiteLLM_VerificationToken" SET token=%s WHERE token=%s',
                ("private-reference-b", "private-reference-a"),
            )
        elif change == "recreate":
            await connection.execute('DELETE FROM "LiteLLM_VerificationToken" WHERE token=%s', ("private-reference-a",))
            await connection.execute(
                """INSERT INTO "LiteLLM_VerificationToken"(token,key_alias,created_at,user_id,team_id)
                VALUES('private-reference-a','Production',%s,'owner-a','team-a')""",
                (NOW + timedelta(seconds=1),),
            )
        elif change == "transfer":
            await connection.execute(
                'UPDATE "LiteLLM_VerificationToken" SET user_id=%s WHERE token=%s', ("owner-b", "private-reference-a")
            )
        else:
            await connection.execute(
                'UPDATE "LiteLLM_VerificationToken" SET team_id=%s WHERE token=%s', ("team-b", "private-reference-a")
            )
    with pytest.raises(HTTPException) as stale_get:
        await runtime.metadata.get("key", _reference(target), ADMIN)
    with pytest.raises(HTTPException) as stale_replay:
        await runtime.metadata.update("key", _reference(target), data, ADMIN, "original")
    assert (stale_get.value.status_code, stale_replay.value.status_code) == (404, 404)
    replacement: Final = await _target(runtime)
    assert replacement["id"] != target["id"] and _JSON.validate_python(replacement["metadata"])["tags"] == []


@pytest.mark.asyncio
async def test_deleted_or_unknown_objects_cannot_create_phantom_metadata(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime, "team")
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('DELETE FROM "LiteLLM_TeamTable" WHERE team_id=%s', ("team-a",))
    for reference in (_reference(target), uuid4()):
        with pytest.raises(HTTPException) as missing:
            await runtime.metadata.update(
                "team", reference, MetadataUpdate(tags=("phantom",), expected_version=0), ADMIN, str(reference)
            )
        assert missing.value.status_code == 404
    async with runtime.store.diagnostic_transaction() as connection:
        counts: Final = await (
            await connection.execute(
                "SELECT (SELECT count(*) FROM retail_object_metadata) AS metadata,(SELECT count(*) FROM retail_operation_events) AS events"
            )
        ).fetchone()
    assert counts == {"metadata": 0, "events": 0}


@pytest.mark.asyncio
async def test_org_feature_off_disables_all_organization_target_access(
    runtime: MetadataRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    target: Final = await _target(runtime, "organization")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "false")
    with pytest.raises(HTTPException) as disabled:
        await runtime.metadata.get("organization", _reference(target), ADMIN)
    assert disabled.value.status_code == 404


@pytest.mark.asyncio
async def test_failed_audit_append_rolls_back_labels_and_receipt(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(
            """CREATE FUNCTION reject_metadata_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected audit failure'; END; $$;
            CREATE TRIGGER reject_metadata_audit BEFORE INSERT ON retail_operation_events
            FOR EACH ROW EXECUTE FUNCTION reject_metadata_audit();""",
            prepare=False,
        )
    with pytest.raises(psycopg.Error):
        await runtime.metadata.update(
            "key", _reference(target), MetadataUpdate(tags=("rollback",), expected_version=0), ADMIN, "rollback"
        )
    current: Final = await runtime.metadata.get("key", _reference(target), ADMIN)
    assert _JSON.validate_python(current["metadata"])["version"] == 0
    async with runtime.store.diagnostic_transaction() as connection:
        receipt: Final = await (
            await connection.execute("SELECT count(*) AS n FROM retail_object_metadata_receipts")
        ).fetchone()
    assert receipt == {"n": 0}


@pytest.mark.asyncio
async def test_native_metadata_and_financial_tables_are_unchanged_by_label_writes(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    await runtime.metadata.update(
        "key", _reference(target), MetadataUpdate(tags=("management-only",), expected_version=0), ADMIN, "label"
    )
    async with await psycopg.AsyncConnection.connect(runtime.native_url, row_factory=dict_row) as connection:
        native: Final = await (
            await connection.execute(
                'SELECT metadata FROM "LiteLLM_VerificationToken" WHERE token=%s', ("private-reference-a",)
            )
        ).fetchone()
    assert native == {"metadata": {"native": "untouched"}}
    async with runtime.store.transaction() as connection:
        finance: Final = await (
            await connection.execute(
                """SELECT (SELECT count(*) FROM retail_wallets) AS wallets,
            (SELECT count(*) FROM retail_ledger) AS ledger,
            (SELECT count(*) FROM retail_reservations) AS reservations"""
            )
        ).fetchone()
    assert finance == {"wallets": 0, "ledger": 0, "reservations": 0}


@pytest.mark.asyncio
async def test_ordinary_key_settings_update_preserves_reference_and_labels(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    await runtime.metadata.update(
        "key", _reference(target), MetadataUpdate(tags=("stable",), expected_version=0), ADMIN, "stable"
    )
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(
            'UPDATE "LiteLLM_VerificationToken" SET key_alias=%s,updated_at=%s WHERE token=%s',
            ("Renamed", NOW + timedelta(days=1), "private-reference-a"),
        )
    current: Final = await runtime.metadata.get("key", _reference(target), ADMIN)
    assert current["id"] == target["id"] and current["name"] == "Renamed"
    assert _JSON.validate_python(current["metadata"])["tags"] == ["stable"]


@pytest.mark.asyncio
async def test_missing_native_authority_returns_503_without_serving_saved_labels(runtime: MetadataRuntime) -> None:
    target: Final = await _target(runtime)
    data: Final = MetadataUpdate(tags=("saved",), expected_version=0)
    await runtime.metadata.update("key", _reference(target), data, ADMIN, "saved")
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('DROP TABLE "LiteLLM_VerificationToken"')
    with pytest.raises(HTTPException) as unavailable:
        await runtime.metadata.get("key", _reference(target), ADMIN)
    with pytest.raises(HTTPException) as replay:
        await runtime.metadata.update("key", _reference(target), data, ADMIN, "saved")
    assert (unavailable.value.status_code, replay.value.status_code) == (503, 503)


@pytest.mark.asyncio
async def test_unobserved_ownership_roundtrip_is_current_state_matching_not_transfer_history(
    runtime: MetadataRuntime,
) -> None:
    target: Final = await _target(runtime)
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(
            'UPDATE "LiteLLM_VerificationToken" SET user_id=%s WHERE token=%s', ("owner-b", "private-reference-a")
        )
        await connection.execute(
            'UPDATE "LiteLLM_VerificationToken" SET user_id=%s WHERE token=%s', ("owner-a", "private-reference-a")
        )
    assert (await runtime.metadata.get("key", _reference(target), ADMIN))["id"] == target["id"]
    page: Final = await runtime.metadata.targets("key", ADMIN)
    assert "unobserved transfer history is not tracked" in str(page["scope_note"])

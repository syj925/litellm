import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr

from litellm.proxy._types import hash_token
from litellm.proxy.retail.native_audit_store import ensure_native_schema
from litellm.proxy.retail.scim_config import ScimConfig, ScimSetup
from litellm.proxy.retail.scim_protocol import (
    PATCH_SCHEMA,
    USER_SCHEMA,
    ScimFailure,
    ScimList,
    ScimProfile,
    ScimQuery,
    ScimRecord,
    etag,
)
from litellm.proxy.retail.scim_store import NativeScimStore


@dataclass(frozen=True, slots=True)
class Writer:
    connection: psycopg.AsyncConnection

    async def query_raw(self, query: str, *args: object) -> object:
        positions: Final = tuple(int(match[1])-1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(re.sub(r"\$\d+", "%s", query), tuple(args[i] for i in positions))
        return await cursor.fetchall()

    async def execute_raw(self, query: str, *args: object) -> int:
        positions: Final = tuple(int(match[1])-1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(re.sub(r"\$\d+", "%s", query),
            tuple(args[i] for i in positions) or None, prepare=False)
        return cursor.rowcount


@dataclass(frozen=True, slots=True)
class Client:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[Writer]:
        async with asyncio.timeout(timeout.total_seconds()):
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                yield Writer(connection)


@dataclass(frozen=True, slots=True)
class Fixture:
    client: Client
    store: NativeScimStore
    setup: ScimSetup


@dataclass(frozen=True, slots=True)
class SnapshotWriter:
    writer: Writer
    mutate: Callable[[], Awaitable[None]]

    async def query_raw(self, query: str, *args: object) -> object:
        result: Final = await self.writer.query_raw(query, *args)
        if query.startswith("SELECT count(*) AS total FROM retail_scim_users"):
            await self.mutate()
        return result

    async def execute_raw(self, query: str, *args: object) -> int:
        return await self.writer.execute_raw(query, *args)


@dataclass(frozen=True, slots=True)
class SnapshotClient:
    client: Client
    mutate: Callable[[], Awaitable[None]]

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[SnapshotWriter]:
        async with self.client.tx(timeout=timeout) as writer:
            yield SnapshotWriter(writer, self.mutate)


def profile(label: str = "one", *, active: bool = True) -> ScimProfile:
    return ScimProfile(external_id="external-"+label, user_name="User-"+label, display_name="Directory "+label, active=active)


def patch(*operations: Mapping[str, object]) -> bytes:
    return json.dumps({"schemas": [PATCH_SCHEMA], "Operations": operations}).encode()


async def created(native: Fixture, label: str = "one", *, active: bool = True) -> ScimRecord:
    result: Final = await native.store.create_user(native.setup, profile(label, active=active))
    assert isinstance(result, ScimRecord)
    return result


async def changed(native: Fixture, record: ScimRecord, body: bytes, if_match: str | None = None) -> ScimRecord:
    result: Final = await native.store.update_user(native.setup, record.id, body, replace=False, if_match=if_match)
    assert isinstance(result, ScimRecord)
    return result


async def issued(native: Fixture, record: ScimRecord, label: str = "one") -> str:
    token: Final = hash_token("fixture-managed-key-"+label)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw('''INSERT INTO "LiteLLM_VerificationToken"(token,user_id,models)
            VALUES($1,$2,ARRAY['fixture-model']) RETURNING token''', token, record.user_id)
    return token


@pytest_asyncio.fixture(loop_scope="function")
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    source: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not source:
        pytest.skip("A dedicated SCIM PostgreSQL service is required")
    base: Final = conninfo_to_dict(source)
    name: Final = "retail_scim_"+uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    client: Final = Client(make_conninfo(**{**base, "dbname": name}))
    store: Final = NativeScimStore(client)
    configuration: Final = ScimSetup(ScimConfig(namespace="fixture-directory", generation=1,
        public_origin="https://gateway.invalid", max_users=1000), SecretStr("rt-scim."+"f"*32))
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    try:
        async with client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.execute_raw('''CREATE TABLE "LiteLLM_UserTable"(
                user_id text PRIMARY KEY,user_role text,password text,password_reset_required boolean,user_email text,
                sso_user_id text UNIQUE,team_id text,organization_id text,object_permission_id text,
                teams text[] DEFAULT '{}',models text[] NOT NULL,metadata jsonb DEFAULT '{}',spend float8 DEFAULT 0)''')
            await writer.execute_raw('''CREATE TABLE "LiteLLM_VerificationToken"(
                token text PRIMARY KEY,user_id text,models text[] NOT NULL,blocked boolean,expires timestamp(3),
                created_at timestamp(3) DEFAULT now(),spend float8 DEFAULT 0)''')
            await writer.execute_raw('''CREATE TABLE retail_wallets(user_id text PRIMARY KEY,balance_nanos bigint);
                INSERT INTO retail_wallets VALUES('ordinary-owner',123456789);
                INSERT INTO "LiteLLM_UserTable"(user_id,user_role,models) VALUES('ordinary-owner','customer','{}')''')
        await ensure_native_schema(client)
        assert await store.initialize() is None
        assert await store.configure(configuration) is None
        yield Fixture(client, store, configuration)
    finally:
        await store.close()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.asyncio
async def test_provision_creates_only_new_customer_without_native_pii_credential_or_wallet(native: Fixture) -> None:
    result: Final = await created(native)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw('SELECT to_jsonb(u) AS value FROM "LiteLLM_UserTable" u WHERE user_id=$1', result.user_id)
        counts: Final = await writer.query_raw('''SELECT (SELECT count(*) FROM "LiteLLM_VerificationToken") AS keys,
            (SELECT count(*) FROM retail_scim_credentials) AS proofs,(SELECT count(*) FROM retail_wallets) AS wallets''')
    assert rows == [{"value": {"user_id": result.user_id, "user_role": "customer", "password": None,
        "password_reset_required": None, "user_email": None, "sso_user_id": None, "team_id": None,
        "organization_id": None, "object_permission_id": None, "teams": [], "models": [], "metadata": {}, "spend": 0}}]
    assert counts == [{"keys": 0, "proofs": 0, "wallets": 1}]
    assert result.user_id != "retail-scim-user:"+str(result.id)
    assert result.external_id not in repr(result) and result.user_id not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ("external_id", "user_name"))
async def test_concurrent_duplicate_identity_has_one_native_account_and_one_conflict(native: Fixture, collision: str) -> None:
    first: Final = profile()
    second: Final = profile("two").model_copy(update={collision: first.external_id if collision == "external_id" else first.user_name.upper()})
    outcomes: Final = await asyncio.gather(native.store.create_user(native.setup, first), native.store.create_user(native.setup, second))
    assert sum(isinstance(row, ScimRecord) for row in outcomes) == 1
    assert outcomes.count(ScimFailure(409, "uniqueness")) == 1
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        counts: Final = await writer.query_raw('''SELECT (SELECT count(*) FROM retail_scim_users) AS resources,
            (SELECT count(*) FROM "LiteLLM_UserTable" WHERE user_id<>'ordinary-owner') AS users''')
    assert counts == [{"resources": 1, "users": 1}]


@pytest.mark.asyncio
async def test_generation_token_namespace_and_retained_cap_fence_old_workers(native: Fixture) -> None:
    assert await native.store.configure(native.setup) is None
    result: Final = await created(native)
    newer: Final = ScimSetup(native.setup.config.model_copy(update={"generation": 2}), SecretStr("rt-scim."+"g"*32))
    assert await native.store.configure(newer) is None
    assert await native.store.get_user(native.setup, result.id) == ScimFailure(503)
    assert await native.store.configure(native.setup) == ScimFailure(409)
    assert await native.store.configure(ScimSetup(newer.config, native.setup.token)) == ScimFailure(409)
    other: Final = ScimSetup(newer.config.model_copy(update={"namespace": "other", "generation": 3}), newer.token)
    assert await native.store.configure(other) == ScimFailure(409)
    assert isinstance(await native.store.get_user(newer, result.id), ScimRecord)


@pytest.mark.asyncio
async def test_resource_cap_includes_tombstones_and_cannot_shrink_below_retained_count(native: Fixture) -> None:
    setup: Final = ScimSetup(native.setup.config.model_copy(update={"generation": 2, "max_users": 1}), native.setup.token)
    assert await native.store.configure(setup) is None
    result: Final = await native.store.create_user(setup, profile())
    assert isinstance(result, ScimRecord)
    assert await native.store.delete_user(setup, result.id, None) is None
    assert await native.store.create_user(setup, profile("two")) == ScimFailure(503)
    expanded: Final = ScimSetup(setup.config.model_copy(update={"generation": 3, "max_users": 2}), setup.token)
    assert await native.store.configure(expanded) is None
    assert isinstance(await native.store.create_user(expanded, profile("two")), ScimRecord)
    assert await native.store.configure(ScimSetup(expanded.config.model_copy(update={"generation": 4, "max_users": 1}), setup.token)) == ScimFailure(409)


@pytest.mark.asyncio
async def test_filter_paging_and_counts_use_exact_scoped_values_without_sql_interpolation(native: Fixture) -> None:
    one: Final = await created(native)
    two: Final = await created(native, "two", active=False)
    first: Final = await native.store.list_users(native.setup, ScimQuery(count=1))
    second: Final = await native.store.list_users(native.setup, ScimQuery(start_index=2, count=1))
    empty: Final = await native.store.list_users(native.setup, ScimQuery(count=0))
    assert first == ScimList(resources=(one,), total=2, start_index=1)
    assert second == ScimList(resources=(two,), total=2, start_index=2)
    assert empty == ScimList(resources=(), total=2, start_index=1)
    enormous: Final = 10**100
    assert await native.store.list_users(native.setup, ScimQuery(start_index=enormous)) == ScimList(
        resources=(), total=2, start_index=enormous,
    )
    for field, value, expected in (("id", str(one.id), one), ("external_id", one.external_id, one),
        ("user_name", one.user_name.lower(), one), ("active", False, two)):
        assert await native.store.list_users(native.setup, ScimQuery(filter_field=field, filter_value=value)) == ScimList(resources=(expected,), total=1, start_index=1)
    assert await native.store.list_users(native.setup, ScimQuery(filter_field="external_id", filter_value="' OR true --")) == ScimList(resources=(), total=0, start_index=1)


@pytest.mark.asyncio
async def test_patch_atomicity_noop_versions_and_concurrent_etag_compare_and_swap(native: Fixture) -> None:
    current: Final = await created(native)
    noop: Final = patch({"op": "replace", "path": "displayName", "value": current.display_name})
    assert await native.store.update_user(native.setup, current.id, noop, replace=False, if_match=etag(current)) == current
    bad: Final = patch({"op": "replace", "path": "displayName", "value": "Changed"}, {"op": "replace", "path": "roles", "value": ["admin"]})
    assert isinstance(await native.store.update_user(native.setup, current.id, bad, replace=False, if_match=None), ScimFailure)
    assert await native.store.get_user(native.setup, current.id) == current
    bodies: Final = (patch({"op": "replace", "path": "displayName", "value": "First"}), patch({"op": "replace", "path": "displayName", "value": "Second"}))
    outcomes: Final = await asyncio.gather(*(native.store.update_user(native.setup, current.id, body, replace=False, if_match=etag(current)) for body in bodies))
    assert sum(isinstance(row, ScimRecord) for row in outcomes) == 1 and outcomes.count(ScimFailure(412)) == 1
    latest: Final = await native.store.get_user(native.setup, current.id)
    assert isinstance(latest, ScimRecord) and latest.version == current.version+1


@pytest.mark.asyncio
async def test_active_transitions_and_single_request_roundtrip_permanently_revoke_prior_proofs(native: Fixture) -> None:
    current: Final = await created(native)
    token: Final = await issued(native, current)
    disabled: Final = await changed(native, current, patch({"op": "replace", "path": "active", "value": False}))
    assert disabled.credential_revision != current.credential_revision
    with pytest.raises(psycopg.errors.RaiseException):
        await issued(native, disabled, "inactive")
    restored: Final = await changed(native, disabled, patch({"op": "replace", "path": "active", "value": True}))
    fresh_token: Final = await issued(native, restored, "new")
    roundtrip: Final = await changed(native, restored, patch({"op": "replace", "path": "active", "value": False}, {"op": "replace", "path": "active", "value": True}))
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        proofs: Final = await writer.query_raw("SELECT token_hash,credential_revision FROM retail_scim_credentials ORDER BY token_hash")
        rows: Final = await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"')
    assert {row["token_hash"] for row in proofs} == {token, fresh_token}
    assert all(row["credential_revision"] != roundtrip.credential_revision for row in proofs)
    assert rows == [{"total": 2}] and roundtrip.active and roundtrip.version == restored.version+1


@pytest.mark.asyncio
async def test_rotation_retains_old_hash_tombstone_and_rejects_reuse_under_any_owner(native: Fixture) -> None:
    current: Final = await created(native)
    original: Final = await issued(native, current)
    replacement: Final = hash_token("fixture-rotated-key")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw('UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1 RETURNING token', original, replacement)
        proofs: Final = await writer.query_raw("SELECT token_hash,credential_revision FROM retail_scim_credentials")
        await writer.query_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1 RETURNING token', replacement)
    assert {row["token_hash"] for row in proofs} == {original, replacement}
    assert all(row["credential_revision"] == current.credential_revision for row in proofs)
    for owner in (current.user_id, "ordinary-owner"):
        for token in (original, replacement):
            with pytest.raises(psycopg.errors.RaiseException):
                async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
                    await writer.query_raw('INSERT INTO "LiteLLM_VerificationToken"(token,user_id,models) VALUES($1,$2,\'{}\') RETURNING token', token, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("blocked", "expired", "revision"))
async def test_revoked_blocked_or_expired_managed_key_cannot_rotate(native: Fixture, change: str) -> None:
    current: Final = await created(native)
    token: Final = await issued(native, current)
    if change == "revision":
        await changed(native, current, patch({"op": "replace", "path": "active", "value": False}, {"op": "replace", "path": "active", "value": True}))
    else:
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET '+("blocked=true" if change == "blocked" else "expires=now()-interval '1 second'"))
    with pytest.raises(psycopg.errors.RaiseException):
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.query_raw('UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1 RETURNING token', token, hash_token("another-key"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("proof_update", "proof_delete", "user_move", "role", "key_move", "reserved_insert"))
async def test_native_identity_and_credential_proofs_cannot_be_reassigned_or_rewritten(native: Fixture, mutation: str) -> None:
    current: Final = await created(native)
    token: Final = await issued(native, current)
    statements: Final = {
        "proof_update": "UPDATE retail_scim_credentials SET credential_revision=gen_random_uuid()",
        "proof_delete": "DELETE FROM retail_scim_credentials",
        "user_move": 'UPDATE "LiteLLM_UserTable" SET user_id=\'ordinary-new\' WHERE user_id=$1',
        "role": 'UPDATE "LiteLLM_UserTable" SET user_role=\'proxy_admin\' WHERE user_id=$1',
        "key_move": 'UPDATE "LiteLLM_VerificationToken" SET user_id=\'ordinary-owner\' WHERE token=$2',
        "reserved_insert": 'INSERT INTO "LiteLLM_UserTable"(user_id,user_role,models) VALUES(\'retail-scim-user:manual\',\'customer\',\'{}\')',
    }
    with pytest.raises(psycopg.errors.RaiseException):
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.execute_raw(statements[mutation], current.user_id, token)


@pytest.mark.asyncio
async def test_delete_retains_finance_native_keys_and_tombstones_but_recreation_is_new_identity(native: Fixture) -> None:
    current: Final = await created(native)
    token: Final = await issued(native, current)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw("INSERT INTO retail_wallets VALUES($1,9999) RETURNING user_id", current.user_id)
    assert await native.store.delete_user(native.setup, current.id, etag(current)) is None
    assert await native.store.get_user(native.setup, current.id) == ScimFailure(404)
    assert await native.store.delete_user(native.setup, current.id, None) == ScimFailure(404)
    replacement: Final = await created(native)
    assert replacement.id != current.id and replacement.user_id != current.user_id
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw('''SELECT (SELECT count(*) FROM "LiteLLM_VerificationToken" WHERE token=$1) AS keys,
            (SELECT count(*) FROM "LiteLLM_UserTable" WHERE user_id=$2) AS users,
            (SELECT balance_nanos FROM retail_wallets WHERE user_id=$2) AS balance,
            (SELECT count(*) FROM retail_scim_credentials WHERE token_hash=$1) AS proofs''', token, current.user_id)
    assert rows == [{"keys": 1, "users": 1, "balance": 9999, "proofs": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ("global", "resource"))
async def test_credential_counter_capacity_rolls_back_key_and_proof_atomically(native: Fixture, which: str) -> None:
    current: Final = await created(native)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("UPDATE retail_scim_settings SET credential_count=1000000" if which == "global" else "UPDATE retail_scim_users SET credential_count=10000")
    with pytest.raises(psycopg.errors.RaiseException):
        await issued(native, current)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw('''SELECT (SELECT count(*) FROM "LiteLLM_VerificationToken") AS keys,
            (SELECT count(*) FROM retail_scim_credentials) AS proofs''')
    assert rows == [{"keys": 0, "proofs": 0}]


@pytest.mark.asyncio
async def test_audit_capacity_rolls_back_profile_revision_and_native_provisioning(native: Fixture) -> None:
    current: Final = await created(native)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("UPDATE retail_native_audit_settings SET retained_count=10000 WHERE singleton")
    assert await native.store.create_user(native.setup, profile("two")) == ScimFailure(503)
    assert await native.store.update_user(native.setup, current.id, patch({"op": "replace", "path": "active", "value": False}), replace=False, if_match=None) == ScimFailure(503)
    assert await native.store.delete_user(native.setup, current.id, None) == ScimFailure(503)
    assert await native.store.get_user(native.setup, current.id) == current
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_UserTable"')
    assert rows == [{"total": 2}]


@pytest.mark.asyncio
async def test_audit_contains_only_service_metadata_and_close_does_not_close_borrowed_client(native: Fixture) -> None:
    current: Final = await created(native)
    await changed(native, current, patch({"op": "replace", "path": "displayName", "value": "Updated"}))
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw("SELECT actor_id,action,affected_count FROM retail_native_audit_outbox ORDER BY occurred_at")
    assert rows == [{"actor_id": "system:scim-directory", "action": action, "affected_count": 1}
        for action in ("native_scim.configured", "native_scim.user_created", "native_scim.user_updated")]
    assert current.external_id not in str(rows) and native.setup.token.get_secret_value() not in str(rows)
    await native.store.close()
    assert await native.store.list_users(native.setup, ScimQuery()) == ScimFailure(503)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        assert await writer.query_raw("SELECT 1 AS alive") == [{"alive": 1}]


@pytest.mark.asyncio
async def test_list_count_and_page_stay_in_one_snapshot_during_concurrent_provisioning(native: Fixture) -> None:
    first: Final = await created(native)

    async def provision_between_count_and_page() -> None:
        await created(native, "concurrent")

    reader: Final = NativeScimStore(SnapshotClient(native.client, provision_between_count_and_page))
    result: Final = await reader.list_users(native.setup, ScimQuery())
    assert result == ScimList(resources=(first,), total=1, start_index=1)
    latest: Final = await native.store.list_users(native.setup, ScimQuery())
    assert isinstance(latest, ScimList) and latest.total == len(latest.resources) == 2


@pytest.mark.asyncio
async def test_replace_clears_omitted_display_and_preserves_immutable_external_identity(native: Fixture) -> None:
    first: Final = await created(native)
    body: Final = json.dumps({"schemas": [USER_SCHEMA], "externalId": first.external_id,
        "userName": "Renamed", "active": False}).encode()
    result: Final = await native.store.update_user(native.setup, first.id, body, replace=True, if_match=etag(first))
    assert isinstance(result, ScimRecord)
    assert result.display_name is None and result.user_name == "Renamed" and not result.active
    assert result.id == first.id and result.user_id == first.user_id and result.external_id == first.external_id
    assert result.credential_revision != first.credential_revision and result.version == first.version+1


@pytest.mark.asyncio
async def test_global_proof_capacity_is_serialized_across_distinct_resources(native: Fixture) -> None:
    first: Final = await created(native)
    second: Final = await created(native, "two")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("UPDATE retail_scim_settings SET credential_count=999999")
    outcomes: Final = await asyncio.gather(issued(native, first, "first"), issued(native, second, "second"), return_exceptions=True)
    assert sum(isinstance(row, str) for row in outcomes) == 1
    assert sum(isinstance(row, psycopg.errors.RaiseException) for row in outcomes) == 1
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw("SELECT credential_count FROM retail_scim_settings")
        proofs: Final = await writer.query_raw("SELECT count(*) AS total FROM retail_scim_credentials")
    assert rows == [{"credential_count": 1000000}] and proofs == [{"total": 1}]

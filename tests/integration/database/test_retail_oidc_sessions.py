import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

import jwt
import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import UserAPIKeyAuth, hash_token
from litellm.proxy.retail.native_audit_store import ensure_native_schema
from litellm.proxy.retail.oidc_sessions import (
    OIDC_SESSION_PREFIX,
    BindingSpec,
    MintedOIDCSession,
    NativeOIDCSessionStore,
    SessionSettings,
    fresh_oidc_session,
)

_MASTER: Final = "fixture-signing-material-long-enough-for-hs256"


@dataclass(frozen=True, slots=True)
class NativeWriter:
    connection: psycopg.AsyncConnection

    async def query_raw(self, query: str, *args: object) -> object:
        positions: Final = tuple(int(match[1]) - 1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(re.sub(r"\$\d+", "%s", query), tuple(args[i] for i in positions))
        return await cursor.fetchall()

    async def execute_raw(self, query: str, *args: object) -> int:
        positions: Final = tuple(int(match[1]) - 1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(
            re.sub(r"\$\d+", "%s", query), tuple(args[i] for i in positions) or None, prepare=False,
        )
        return cursor.rowcount


@dataclass(frozen=True, slots=True)
class NativeClient:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta = timedelta(seconds=8)) -> AsyncIterator[NativeWriter]:
        async with asyncio.timeout(timeout.total_seconds()):
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                yield NativeWriter(connection)


@dataclass(frozen=True, slots=True)
class Fixture:
    client: NativeClient
    store: NativeOIDCSessionStore
    settings: SessionSettings


@pytest_asyncio.fixture(loop_scope="function")
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated native OIDC PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_oidc_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    client: Final = NativeClient(make_conninfo(**{**base, "dbname": name}))
    store: Final = NativeOIDCSessionStore(client)
    settings: Final = SessionSettings(
        namespace="fixture-authority", generation=1, digest="a" * 64,
        issuer="https://fixture-idp.invalid", session_seconds=300,
        bindings=(BindingSpec(subject="fixture-subject", user_id="fixture-owner"),),
    )
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    try:
        async with client.tx() as writer:
            await writer.execute_raw('''CREATE TABLE "LiteLLM_UserTable"(
                user_id text PRIMARY KEY,user_role text,user_email text,password text,password_reset_required boolean)''')
            await writer.execute_raw('''CREATE TABLE "LiteLLM_VerificationToken"(
                token text PRIMARY KEY,user_id text,team_id text,metadata jsonb DEFAULT '{}',
                expires timestamp(3),models text[] DEFAULT '{}',allowed_routes text[] DEFAULT '{}',
                key_type text,blocked boolean,created_by text,updated_by text,permissions jsonb DEFAULT '{}',
                config jsonb DEFAULT '{}',aliases jsonb DEFAULT '{}',organization_id text,spend double precision DEFAULT 0)''')
            await writer.execute_raw('''INSERT INTO "LiteLLM_UserTable" VALUES
                ('fixture-owner','proxy_admin','fixture-email','fixture-password',false),
                ('fixture-other','internal_user','fixture-other-email','fixture-other-password',false)''')
        await ensure_native_schema(client)
        await store.initialize()
        await store.configure(settings)
        yield Fixture(client, store, settings)
    finally:
        await store.close()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def mint(native: Fixture, settings: SessionSettings | None = None) -> MintedOIDCSession:
    return await native.store.mint(
        settings or native.settings, "fixture-subject", general_settings={}, premium_user=False, master_key=_MASTER,
    )


def credential(session: MintedOIDCSession) -> str:
    claims: Final = jwt.decode(session.jwt_token, _MASTER, algorithms=["HS256"])
    value: Final = claims["key"]
    assert isinstance(value, str) and value.startswith(OIDC_SESSION_PREFIX)
    return value


async def fresh(native: Fixture, key: str) -> UserAPIKeyAuth:
    async with native.client.tx() as writer:
        return await fresh_oidc_session(
            UserAPIKeyAuth(user_id="fixture-owner", user_role="proxy_admin", metadata={}), key,
            "/key/list", "GET", reader=writer,
        )


async def assert_inactive(native: Fixture, key: str) -> None:
    with pytest.raises(HTTPException) as error:
        await fresh(native, key)
    assert error.value.status_code == 401


def generation(native: Fixture, number: int, *, enabled: bool = True) -> SessionSettings:
    return native.settings.model_copy(update={
        "generation": number, "digest": format(number, "064x"),
        "bindings": (BindingSpec(subject="fixture-subject", user_id="fixture-owner", enabled=enabled),),
    })


@pytest.mark.asyncio
async def test_mint_reuses_native_user_and_freezes_private_proof_and_audit_in_one_transaction(native: Fixture) -> None:
    session: Final = await mint(native)
    key: Final = credential(session)
    claims: Final = jwt.decode(session.jwt_token, _MASTER, algorithms=["HS256"])
    assert claims["login_method"] == "sso" and claims["premium_user"] is False
    assert claims["exp"] <= int(session.expires_at.timestamp())
    auth: Final = await fresh(native, key)
    assert auth.user_role == "proxy_admin" and auth.team_id == UI_SESSION_TOKEN_TEAM_ID
    assert auth.metadata["login_method"] == "retail_oidc"
    async with native.client.tx() as writer:
        users: Final = await writer.query_raw('SELECT user_id,user_role,password FROM "LiteLLM_UserTable" ORDER BY user_id')
        events: Final = await writer.query_raw("SELECT action,actor_id,affected_count FROM retail_native_audit_outbox ORDER BY action")
        stored: Final = await writer.query_raw("SELECT token_hash,expires_at FROM retail_oidc_sessions")
    assert users == [
        {"user_id": "fixture-other", "user_role": "internal_user", "password": "fixture-other-password"},
        {"user_id": "fixture-owner", "user_role": "proxy_admin", "password": "fixture-password"},
    ]
    assert events == [
        {"action": "native_identity.configured", "actor_id": "system:oidc-configuration", "affected_count": 1},
        {"action": "native_identity.signed_in", "actor_id": "fixture-owner", "affected_count": 1},
    ]
    assert stored == [{"token_hash": hash_token(key), "expires_at": session.expires_at}]


@pytest.mark.asyncio
async def test_configuration_idempotence_and_stale_worker_fencing(native: Fixture) -> None:
    old: Final = credential(await mint(native))
    await native.store.configure(native.settings)
    await native.store.initialize()
    assert (await fresh(native, old)).user_id == "fixture-owner"
    replacement: Final = generation(native, 2)
    await native.store.configure(replacement)
    await assert_inactive(native, old)
    for operation in (native.store.configure(native.settings), mint(native)):
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 409
    assert (await fresh(native, credential(await mint(native, replacement)))).user_id == "fixture-owner"


@pytest.mark.asyncio
async def test_disable_then_reenable_never_resurrects_old_session(native: Fixture) -> None:
    old: Final = credential(await mint(native))
    await native.store.configure(generation(native, 2, enabled=False))
    await assert_inactive(native, old)
    enabled: Final = generation(native, 3)
    await native.store.configure(enabled)
    await assert_inactive(native, old)
    assert (await fresh(native, credential(await mint(native, enabled)))).user_id == "fixture-owner"


@pytest.mark.asyncio
async def test_subject_cannot_be_rebound_and_failed_config_is_fully_rolled_back(native: Fixture) -> None:
    old: Final = credential(await mint(native))
    conflicting: Final = generation(native, 2).model_copy(update={
        "bindings": (BindingSpec(subject="fixture-subject", user_id="fixture-other"),),
    })
    with pytest.raises(HTTPException) as error:
        await native.store.configure(conflicting)
    assert error.value.status_code == 409
    assert (await fresh(native, old)).user_id == "fixture-owner"
    assert (await fresh(native, credential(await mint(native)))).user_id == "fixture-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", [
    'UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token=$1',
    'UPDATE "LiteLLM_VerificationToken" SET expires=clock_timestamp()-interval \'1 second\' WHERE token=$1',
    'DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1',
])
async def test_native_key_revocation_and_expiry_permanently_kill_session_proof(native: Fixture, statement: str) -> None:
    key: Final = credential(await mint(native))
    async with native.client.tx() as writer:
        await writer.execute_raw(statement, hash_token(key))
    await assert_inactive(native, key)
    async with native.client.tx() as writer:
        await writer.execute_raw(
            'UPDATE "LiteLLM_VerificationToken" SET blocked=false,expires=clock_timestamp()+interval \'1 hour\' WHERE token=$1',
            hash_token(key),
        )
    await assert_inactive(native, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("statement,restore", [
    ('UPDATE "LiteLLM_UserTable" SET password_reset_required=true WHERE user_id=\'fixture-owner\'',
     'UPDATE "LiteLLM_UserTable" SET password_reset_required=false WHERE user_id=\'fixture-owner\''),
    ('UPDATE "LiteLLM_UserTable" SET user_role=\'org_admin\' WHERE user_id=\'fixture-owner\'',
     'UPDATE "LiteLLM_UserTable" SET user_role=\'proxy_admin\' WHERE user_id=\'fixture-owner\''),
    ('UPDATE "LiteLLM_UserTable" SET password=\'replacement-fixture\' WHERE user_id=\'fixture-owner\'',
     'UPDATE "LiteLLM_UserTable" SET password=\'fixture-password\' WHERE user_id=\'fixture-owner\''),
])
async def test_forced_reset_unsupported_role_and_password_changes_do_not_revive_sessions(
    native: Fixture, statement: str, restore: str,
) -> None:
    key: Final = credential(await mint(native))
    async with native.client.tx() as writer:
        await writer.execute_raw(statement)
    await assert_inactive(native, key)
    async with native.client.tx() as writer:
        await writer.execute_raw(restore)
    await assert_inactive(native, key)


@pytest.mark.asyncio
async def test_role_demotion_is_read_fresh_despite_admin_in_cached_auth(native: Fixture) -> None:
    key: Final = credential(await mint(native))
    async with native.client.tx() as writer:
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET user_role=\'proxy_admin_viewer\' WHERE user_id=\'fixture-owner\'')
    await assert_inactive(native, key)
    replacement: Final = credential(await mint(native))
    assert (await fresh(native, replacement)).user_role == "proxy_admin_viewer"
    async with native.client.tx() as writer:
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET user_role=\'customer\' WHERE user_id=\'fixture-owner\'')
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET user_role=\'proxy_admin\' WHERE user_id=\'fixture-owner\'')
    await assert_inactive(native, key)
    await assert_inactive(native, replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    "metadata='{}'::jsonb", "allowed_routes=ARRAY['llm_api_routes']", "token='fixture-regenerated-hash'",
    "team_id=NULL", "user_id='fixture-other'", "models=ARRAY['fixture-model']",
])
async def test_database_guards_reject_session_mutation_and_rotation_without_breaking_original(
    native: Fixture, update: str,
) -> None:
    key: Final = credential(await mint(native))
    with pytest.raises(psycopg.errors.RaiseException, match="identity and route scope"):
        async with native.client.tx() as writer:
            await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET ' + update + " WHERE token=$1", hash_token(key))
    assert (await fresh(native, key)).user_id == "fixture-owner"


@pytest.mark.asyncio
async def test_audit_queue_failure_rolls_back_minted_key_and_proof(native: Fixture) -> None:
    async with native.client.tx() as writer:
        await writer.execute_raw("UPDATE retail_native_audit_settings SET retained_count=10000")
    with pytest.raises(HTTPException) as error:
        await mint(native)
    assert error.value.status_code == 503
    async with native.client.tx() as writer:
        assert await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"') == [{"total": 0}]
        assert await writer.query_raw("SELECT count(*) AS total FROM retail_oidc_sessions") == [{"total": 0}]


@pytest.mark.asyncio
async def test_unknown_subject_and_reset_required_accounts_do_not_mint(native: Fixture) -> None:
    with pytest.raises(HTTPException) as unknown:
        await native.store.mint(
            native.settings, "unmapped-subject", general_settings={}, premium_user=False, master_key=_MASTER,
        )
    assert unknown.value.status_code == 401
    async with native.client.tx() as writer:
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET password_reset_required=true WHERE user_id=\'fixture-owner\'')
    with pytest.raises(HTTPException) as reset:
        await mint(native)
    assert reset.value.status_code == 401


@pytest.mark.asyncio
async def test_session_proof_is_immutable_and_user_deletion_does_not_rebind_it(native: Fixture) -> None:
    key: Final = credential(await mint(native))
    with pytest.raises(psycopg.errors.RaiseException, match="proof cannot be changed"):
        async with native.client.tx() as writer:
            await writer.execute_raw("UPDATE retail_oidc_sessions SET generation=generation+1")
    async with native.client.tx() as writer:
        await writer.execute_raw('DELETE FROM "LiteLLM_UserTable" WHERE user_id=\'fixture-owner\'')
        await writer.execute_raw('''INSERT INTO "LiteLLM_UserTable" VALUES
            ('fixture-owner','proxy_admin','recreated-fixture','recreated-password',false)''')
    await assert_inactive(native, key)


@pytest.mark.asyncio
async def test_concurrent_login_and_generation_change_have_one_linearized_native_authority(native: Fixture) -> None:
    current: Final = generation(native, 2)
    outcomes: Final = await asyncio.gather(mint(native), native.store.configure(current), return_exceptions=True)
    assert outcomes[1] is None
    if isinstance(outcomes[0], MintedOIDCSession):
        await assert_inactive(native, credential(outcomes[0]))
    else:
        assert isinstance(outcomes[0], HTTPException) and outcomes[0].status_code == 409
    assert (await fresh(native, credential(await mint(native, current)))).user_id == "fixture-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["proxy_admin", "proxy_admin_viewer", "internal_user", "internal_user_viewer", "customer"])
async def test_only_native_portal_role_is_minted_without_claim_role_input(native: Fixture, role: str) -> None:
    async with native.client.tx() as writer:
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET user_role=$1 WHERE user_id=\'fixture-owner\'', role)
    session: Final = await mint(native)
    assert jwt.decode(session.jwt_token, _MASTER, algorithms=["HS256"])["user_role"] == role
    assert (await fresh(native, credential(session))).user_role == role


@pytest.mark.asyncio
async def test_config_audit_failure_and_same_generation_changes_do_not_modify_effective_authority(native: Fixture) -> None:
    key: Final = credential(await mint(native))
    different: Final = native.settings.model_copy(update={"digest": "b" * 64})
    with pytest.raises(HTTPException) as conflict:
        await native.store.configure(different)
    assert conflict.value.status_code == 409
    async with native.client.tx() as writer:
        await writer.execute_raw("UPDATE retail_native_audit_settings SET retained_count=10000")
    with pytest.raises(HTTPException) as failure:
        await native.store.configure(generation(native, 2, enabled=False))
    assert failure.value.status_code == 503
    assert (await fresh(native, key)).user_id == "fixture-owner"


@pytest.mark.asyncio
async def test_ui_encoding_failure_before_commit_rolls_back_key_proof_and_audit(native: Fixture) -> None:
    with pytest.raises(UnicodeError):
        await native.store.mint(
            native.settings, "fixture-subject", general_settings={}, premium_user=False, master_key="\ud800",
        )
    async with native.client.tx() as writer:
        assert await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"') == [{"total": 0}]
        assert await writer.query_raw("SELECT count(*) AS total FROM retail_oidc_sessions") == [{"total": 0}]
        assert await writer.query_raw("SELECT count(*) AS total FROM retail_native_audit_outbox") == [{"total": 1}]


@pytest.mark.asyncio
async def test_elapsed_session_expiry_is_checked_even_with_intact_cached_admin_and_native_proof(native: Fixture) -> None:
    source: Final = credential(await mint(native))
    expired: Final = OIDC_SESSION_PREFIX + "fixture-expired-session"
    identifier: Final = str(uuid4())
    async with native.client.tx() as writer:
        await writer.execute_raw("SET LOCAL retail.oidc_mint='on'")
        await writer.execute_raw('''INSERT INTO "LiteLLM_VerificationToken"
            (token,user_id,team_id,metadata,expires,models,allowed_routes,key_type)
            SELECT $1,user_id,team_id,jsonb_build_object('login_method','retail_oidc','oidc_session_id',$2::text),
            clock_timestamp()-interval '1 second',models,allowed_routes,key_type
            FROM "LiteLLM_VerificationToken" WHERE token=$3''', hash_token(expired), identifier, hash_token(source))
        await writer.execute_raw('''INSERT INTO retail_oidc_sessions
            SELECT $1,$2::uuid,p.namespace,p.generation,p.digest,p.binding_id,p.binding_revision,p.user_id,
            k.expires,clock_timestamp() FROM retail_oidc_sessions p
            JOIN "LiteLLM_VerificationToken" k ON k.token=$1 WHERE p.token_hash=$3''',
            hash_token(expired), identifier, hash_token(source))
    await assert_inactive(native, expired)
    assert (await fresh(native, source)).user_role == "proxy_admin"


@pytest.mark.asyncio
async def test_missing_gateway_signing_authority_does_not_create_a_session(native: Fixture) -> None:
    with pytest.raises(HTTPException) as error:
        await native.store.mint(
            native.settings, "fixture-subject", general_settings={}, premium_user=False, master_key="",
        )
    assert error.value.status_code == 503
    async with native.client.tx() as writer:
        assert await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"') == [{"total": 0}]
        assert await writer.query_raw("SELECT count(*) AS total FROM retail_oidc_sessions") == [{"total": 0}]


@pytest.mark.asyncio
async def test_account_session_capacity_fails_closed_and_revocation_frees_a_slot(native: Fixture) -> None:
    original: Final = credential(await mint(native))
    async with native.client.tx() as writer:
        await writer.execute_raw("SET LOCAL retail.oidc_mint='on'")
        await writer.execute_raw('''INSERT INTO "LiteLLM_VerificationToken"
            (token,user_id,team_id,metadata,expires,models,allowed_routes,key_type)
            SELECT 'fixture-capacity-'||i::text,user_id,team_id,
            jsonb_build_object('login_method','retail_oidc','oidc_session_id',gen_random_uuid()::text),
            expires,models,allowed_routes,key_type FROM "LiteLLM_VerificationToken"
            CROSS JOIN generate_series(1,127) i WHERE token=$1''', hash_token(original))
        await writer.execute_raw('''INSERT INTO retail_oidc_sessions
            SELECT k.token,(k.metadata->>'oidc_session_id')::uuid,p.namespace,p.generation,p.digest,
            p.binding_id,p.binding_revision,p.user_id,p.expires_at,clock_timestamp()
            FROM retail_oidc_sessions p CROSS JOIN "LiteLLM_VerificationToken" k
            WHERE p.token_hash=$1 AND k.token<>$1''', hash_token(original))
    with pytest.raises(HTTPException) as error:
        await mint(native)
    assert error.value.status_code == 503
    async with native.client.tx() as writer:
        assert await writer.query_raw("SELECT count(*) AS total FROM retail_oidc_sessions") == [{"total": 128}]
        await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token=$1', hash_token(original))
    assert (await fresh(native, credential(await mint(native)))).user_id == "fixture-owner"
    await assert_inactive(native, original)


@pytest.mark.asyncio
async def test_binding_revision_prevents_restore_even_without_a_settings_generation_change(native: Fixture) -> None:
    key: Final = credential(await mint(native))
    async with native.client.tx() as writer:
        await writer.execute_raw("UPDATE retail_oidc_bindings SET enabled=false")
    await assert_inactive(native, key)
    async with native.client.tx() as writer:
        await writer.execute_raw("UPDATE retail_oidc_bindings SET enabled=true")
    await assert_inactive(native, key)
    assert (await fresh(native, credential(await mint(native)))).user_id == "fixture-owner"


@pytest.mark.asyncio
async def test_binding_native_identity_is_immutable_at_the_database_boundary(native: Fixture) -> None:
    key: Final = credential(await mint(native))
    with pytest.raises(psycopg.errors.RaiseException, match="identity cannot be reassigned"):
        async with native.client.tx() as writer:
            await writer.execute_raw("UPDATE retail_oidc_bindings SET user_id='fixture-other'")
    assert (await fresh(native, key)).user_id == "fixture-owner"

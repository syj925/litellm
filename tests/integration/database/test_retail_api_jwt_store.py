import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr

from litellm.proxy._types import hash_token
from litellm.proxy.retail.api_jwt_config import ApiJwtBinding, ApiJwtConfig, ApiJwtFailure
from litellm.proxy.retail.api_jwt_provider import VerifiedApiJwt
from litellm.proxy.retail.api_jwt_store import NativeApiJwtStore, ResolvedApiJwt
from litellm.proxy.retail.native_audit_store import ensure_native_schema


@dataclass(frozen=True, slots=True)
class Writer:
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
class Client:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[Writer]:
        async with asyncio.timeout(timeout.total_seconds()):
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                yield Writer(connection)


@dataclass(frozen=True, slots=True)
class PrivateWriter:
    writer: Writer
    subject: str

    async def query_raw(self, query: str, *args: object) -> object:
        assert self.subject not in query and self.subject not in str(args)
        return await self.writer.query_raw(query, *args)

    async def execute_raw(self, query: str, *args: object) -> int:
        assert self.subject not in query and self.subject not in str(args)
        return await self.writer.execute_raw(query, *args)


@dataclass(frozen=True, slots=True)
class PrivateClient:
    client: Client
    subject: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[PrivateWriter]:
        async with self.client.tx(timeout=timeout) as writer:
            yield PrivateWriter(writer, self.subject)


@dataclass(frozen=True, slots=True)
class Fixture:
    client: Client
    store: NativeApiJwtStore
    config: ApiJwtConfig

    def token(self, revision: UUID | None = None) -> VerifiedApiJwt:
        return VerifiedApiJwt(
            SecretStr(self.config.bindings[0].subject), SecretStr(self.config.client_id),
            revision or self.config.bindings[0].binding_revision, int(datetime.now(timezone.utc).timestamp()),
            datetime.now(timezone.utc) + timedelta(seconds=300),
        )


@pytest_asyncio.fixture(loop_scope="function")
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    source: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not source:
        pytest.skip("A dedicated API identity PostgreSQL service is required")
    base: Final = conninfo_to_dict(source)
    name: Final = "retail_api_jwt_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    client: Final = Client(make_conninfo(**{**base, "dbname": name}))
    store: Final = NativeApiJwtStore(client)
    config: Final = ApiJwtConfig(
        namespace="fixture-api", generation=1, issuer="https://issuer.invalid", audience="fixture-api",
        client_id="fixture-client", jwks_uri="https://issuer.invalid/jwks",
        bindings=(ApiJwtBinding(subject="fixture-subject", user_id="fixture-owner",
            token_hash=hash_token("fixture-key"), binding_revision=uuid4()),),
    )
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    try:
        async with client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.execute_raw('''CREATE TABLE "LiteLLM_UserTable"(
                user_id text PRIMARY KEY,user_role text,user_email text,password text,password_reset_required boolean,
                models text[] DEFAULT '{}',metadata jsonb DEFAULT '{}',teams text[] DEFAULT '{}',team_id text,
                organization_id text,object_permission_id text,spend double precision DEFAULT 0,
                max_budget double precision,rpm_limit bigint,tpm_limit bigint)''')
            await writer.execute_raw('''CREATE TABLE "LiteLLM_VerificationToken"(
                token text PRIMARY KEY,user_id text,team_id text,organization_id text,project_id text,
                object_permission_id text,agent_id text,metadata jsonb DEFAULT '{}',key_type text,
                allowed_routes text[] DEFAULT '{}',models text[] DEFAULT '{}',expires timestamp(3),blocked boolean,
                spend double precision DEFAULT 0,max_budget double precision,rpm_limit bigint,tpm_limit bigint,
                model_max_budget jsonb DEFAULT '{}',aliases jsonb DEFAULT '{}',permissions jsonb DEFAULT '{}',
                config jsonb DEFAULT '{}',budget_limits jsonb,access_group_ids text[] DEFAULT '{}')''')
            await writer.execute_raw('''INSERT INTO "LiteLLM_UserTable"
                (user_id,user_role,user_email,password,password_reset_required,models,max_budget,rpm_limit,tpm_limit)
                VALUES('fixture-owner','customer','owner@example.invalid','private-password',false,
                ARRAY['fixture-model'],8.5,7,700),('fixture-other','internal_user','other@example.invalid',
                'other-password',false,ARRAY['fixture-model'],4,4,400)''')
            await writer.query_raw('''INSERT INTO "LiteLLM_VerificationToken"
                (token,user_id,key_type,models,max_budget,rpm_limit,tpm_limit,metadata,model_max_budget)
                VALUES($1,'fixture-owner','llm_api',ARRAY['fixture-model'],2.5,3,300,
                '{"retail_resource_policy":{"mode":"auto"}}','{"fixture-model":1.25}') RETURNING token''',
                config.bindings[0].token_hash,
            )
        await ensure_native_schema(client)
        assert await store.initialize() is None
        assert await store.configure(config) is None
        yield Fixture(client, store, config)
    finally:
        await store.close()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def changed(native: Fixture, binding: ApiJwtBinding, generation: int = 2) -> ApiJwtConfig:
    return native.config.model_copy(update={"generation": generation, "bindings": (binding,)})


@pytest.mark.asyncio
async def test_fresh_preserves_native_limits_and_ownership_without_mutating_native_objects(native: Fixture) -> None:
    result: Final = await native.store.fresh(native.config, native.token())
    assert isinstance(result, ResolvedApiJwt)
    assert result.auth.token == result.auth.api_key == native.config.bindings[0].token_hash
    assert result.auth.user_id == result.user.user_id == "fixture-owner"
    assert result.auth.user_role == result.user.user_role == "customer"
    assert result.auth.models == result.user.models == ["fixture-model"]
    assert (result.auth.max_budget, result.auth.rpm_limit, result.auth.tpm_limit) == (2.5, 3, 300)
    assert result.auth.model_max_budget == {"fixture-model": 1.25}
    assert result.auth.metadata == {"retail_resource_policy": {"mode": "auto"}}
    assert (result.user.max_budget, result.user.rpm_limit, result.user.tpm_limit) == (8.5, 7, 700)
    assert result.auth.via_virtual_key is True and result.auth.jwt_claims is None
    assert result.user.password is None and "fixture-owner" not in repr(result)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        events: Final = await writer.query_raw("SELECT action,actor_id,affected_count FROM retail_native_audit_outbox")
        keys: Final = await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"')
        users: Final = await writer.query_raw('SELECT password FROM "LiteLLM_UserTable" WHERE user_id=\'fixture-owner\'')
    assert events == [{"action": "native_api_identity.configured", "actor_id": "system:api-identity-configuration",
        "affected_count": 1}]
    assert keys == [{"total": 1}] and users == [{"password": "private-password"}]


@pytest.mark.asyncio
async def test_configuration_generation_fences_stale_workers_and_remains_idempotent(native: Fixture) -> None:
    await native.store.initialize()
    assert await native.store.configure(native.config) is None
    replacement: Final = native.config.model_copy(update={"generation": 2})
    assert await native.store.configure(replacement) is None
    assert await native.store.configure(native.config) == ApiJwtFailure("invalid")
    assert await native.store.fresh(native.config, native.token()) == ApiJwtFailure("invalid")
    assert isinstance(await native.store.fresh(replacement, native.token()), ResolvedApiJwt)
    same_generation: Final = replacement.model_copy(update={"required_scope": "changed:scope"})
    assert await native.store.configure(same_generation) == ApiJwtFailure("invalid")


@pytest.mark.asyncio
async def test_disabled_binding_cannot_restore_a_previously_used_revision(native: Fixture) -> None:
    original: Final = native.config.bindings[0]
    disabled: Final = changed(native, original.model_copy(update={"enabled": False, "binding_revision": uuid4()}))
    assert await native.store.configure(disabled) is None
    assert await native.store.fresh(disabled, native.token()) == ApiJwtFailure("invalid")
    assert await native.store.configure(changed(native, original, 3)) == ApiJwtFailure("invalid")
    enabled: Final = changed(native, original.model_copy(update={"binding_revision": uuid4()}), 3)
    assert await native.store.configure(enabled) is None
    assert await native.store.fresh(enabled, native.token()) == ApiJwtFailure("invalid")
    assert isinstance(await native.store.fresh(enabled, native.token(enabled.bindings[0].binding_revision)), ResolvedApiJwt)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("subject", "namespace", "token_hash", "user_id", "enabled"))
async def test_any_changed_binding_cannot_recycle_the_current_revision(native: Fixture, change: str) -> None:
    original: Final = native.config.bindings[0]
    if change == "namespace":
        replacement: Final = native.config.model_copy(update={"generation": 2, "namespace": "another-namespace"})
    else:
        values: Final = {"subject": "another-subject", "token_hash": "b" * 64,
            "user_id": "fixture-other", "enabled": False}
        replacement: Final = changed(native, original.model_copy(update={change: values[change]}))
    assert await native.store.configure(replacement) == ApiJwtFailure("invalid")
    assert isinstance(await native.store.fresh(native.config, native.token()), ResolvedApiJwt)


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", (
    '''UPDATE "LiteLLM_VerificationToken" SET blocked=true''',
    '''UPDATE "LiteLLM_VerificationToken" SET expires=now()-interval '1 second' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET user_id='fixture-other' ''',
    '''DELETE FROM "LiteLLM_VerificationToken"''',
    '''UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin' WHERE user_id='fixture-owner' ''',
    '''UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin_viewer' WHERE user_id='fixture-owner' ''',
    '''UPDATE "LiteLLM_UserTable" SET user_role='internal_user_viewer' WHERE user_id='fixture-owner' ''',
    '''UPDATE "LiteLLM_UserTable" SET password_reset_required=true WHERE user_id='fixture-owner' ''',
    '''UPDATE "LiteLLM_UserTable" SET metadata='{"scim_active":false}' WHERE user_id='fixture-owner' ''',
    '''DELETE FROM "LiteLLM_UserTable" WHERE user_id='fixture-owner' ''',
))
async def test_fresh_checks_current_native_authority_without_cache(native: Fixture, statement: str) -> None:
    assert isinstance(await native.store.fresh(native.config, native.token()), ResolvedApiJwt)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw(statement)
    assert await native.store.fresh(native.config, native.token()) == ApiJwtFailure("invalid")


@pytest.mark.asyncio
async def test_key_unblock_is_temporary_suspension_not_permanent_binding_revocation(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET blocked=true')
    token: Final = native.token()
    assert await native.store.fresh(native.config, token) == ApiJwtFailure("invalid")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET blocked=false')
    assert isinstance(await native.store.fresh(native.config, token), ResolvedApiJwt)


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", (
    '''UPDATE "LiteLLM_VerificationToken" SET team_id='some-team' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET organization_id='some-org' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET project_id='some-project' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET object_permission_id='some-permission' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET agent_id='some-agent' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET metadata='{"service_account_id":"some-account"}' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET metadata='{"login_method":"retail_oidc"}' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET key_type='management' ''',
    '''UPDATE "LiteLLM_VerificationToken" SET allowed_routes=ARRAY['management_routes'] ''',
    '''UPDATE "LiteLLM_VerificationToken" SET access_group_ids=ARRAY['some-group'] ''',
    '''UPDATE "LiteLLM_UserTable" SET organization_id='some-org' WHERE user_id='fixture-owner' ''',
    '''UPDATE "LiteLLM_UserTable" SET teams=ARRAY['some-team'] WHERE user_id='fixture-owner' ''',
))
async def test_privilege_graph_changes_are_not_inherited_from_target(native: Fixture, statement: str) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw(statement)
    assert await native.store.fresh(native.config, native.token()) == ApiJwtFailure("invalid")


@pytest.mark.asyncio
async def test_failed_configuration_rolls_back_revisions_bindings_and_audit(native: Fixture) -> None:
    good: Final = native.config.bindings[0].model_copy(update={"binding_revision": uuid4()})
    bad: Final = ApiJwtBinding(subject="invalid-target", user_id="missing", token_hash="b" * 64,
        binding_revision=uuid4())
    replacement: Final = native.config.model_copy(update={"generation": 2, "bindings": (good, bad)})
    assert await native.store.configure(replacement) == ApiJwtFailure("invalid")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        counts: Final = await writer.query_raw('''SELECT
            (SELECT count(*) FROM retail_api_jwt_revisions) AS revisions,
            (SELECT count(*) FROM retail_api_jwt_bindings) AS bindings,
            (SELECT count(*) FROM retail_native_audit_outbox) AS events''')
    assert counts == [{"revisions": 1, "bindings": 1, "events": 1}]
    assert isinstance(await native.store.fresh(native.config, native.token()), ResolvedApiJwt)


@pytest.mark.asyncio
async def test_used_revision_history_is_immutable_and_capacity_fails_without_partial_changes(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('''INSERT INTO retail_api_jwt_revisions(revision)
            SELECT gen_random_uuid() FROM generate_series(1,9999)''')
    replacement: Final = changed(native, native.config.bindings[0].model_copy(update={"binding_revision": uuid4()}))
    assert await native.store.configure(replacement) == ApiJwtFailure("unavailable")
    for statement in ("DELETE FROM retail_api_jwt_revisions", "UPDATE retail_api_jwt_revisions SET revision=gen_random_uuid()"):
        with pytest.raises(psycopg.errors.RaiseException):
            async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
                await writer.execute_raw(statement)
    assert isinstance(await native.store.fresh(native.config, native.token()), ResolvedApiJwt)


@pytest.mark.asyncio
async def test_expired_verification_and_different_client_are_rejected_at_native_admission(native: Fixture) -> None:
    expired: Final = VerifiedApiJwt(SecretStr("fixture-subject"), SecretStr(native.config.client_id),
        native.config.bindings[0].binding_revision, 1, datetime.now(timezone.utc)-timedelta(seconds=1))
    other: Final = VerifiedApiJwt(SecretStr("fixture-subject"), SecretStr("different-client"),
        native.config.bindings[0].binding_revision, 1, datetime.now(timezone.utc)+timedelta(seconds=100))
    assert await native.store.fresh(native.config, expired) == ApiJwtFailure("invalid")
    assert await native.store.fresh(native.config, other) == ApiJwtFailure("invalid")


@pytest.mark.asyncio
async def test_audit_queue_capacity_rolls_configuration_back(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("UPDATE retail_native_audit_settings SET retained_count=10000 WHERE singleton")
    replacement: Final = changed(native, native.config.bindings[0].model_copy(update={"binding_revision": uuid4()}))
    assert await native.store.configure(replacement) == ApiJwtFailure("unavailable")
    assert isinstance(await native.store.fresh(native.config, native.token()), ResolvedApiJwt)


@pytest.mark.asyncio
async def test_closed_store_refuses_new_work_without_closing_native_client(native: Fixture) -> None:
    await native.store.close()
    assert await native.store.initialize() == ApiJwtFailure("unavailable")
    assert await native.store.configure(native.config) == ApiJwtFailure("unavailable")
    assert await native.store.fresh(native.config, native.token()) == ApiJwtFailure("unavailable")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        assert await writer.query_raw("SELECT 1 AS alive") == [{"alive": 1}]


@pytest.mark.asyncio
async def test_external_subject_never_enters_native_driver_parameters_or_stored_binding(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw("SELECT to_jsonb(b) AS value FROM retail_api_jwt_bindings b")
    assert native.config.bindings[0].subject not in str(rows)
    private: Final = NativeApiJwtStore(PrivateClient(native.client, native.config.bindings[0].subject))
    replacement: Final = native.config.model_copy(update={"generation": 2})
    assert await private.configure(replacement) is None
    assert isinstance(await private.fresh(replacement, native.token()), ResolvedApiJwt)


@pytest.mark.asyncio
async def test_concurrent_configuration_has_one_winner_without_leaking_revision_history(native: Fixture) -> None:
    first: Final = changed(native, native.config.bindings[0].model_copy(update={"binding_revision": uuid4()}))
    second: Final = changed(native, native.config.bindings[0].model_copy(update={"binding_revision": uuid4()}))
    outcomes: Final = await asyncio.gather(native.store.configure(first), native.store.configure(second))
    assert outcomes.count(None) == 1 and outcomes.count(ApiJwtFailure("invalid")) == 1
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        counts: Final = await writer.query_raw('''SELECT
            (SELECT count(*) FROM retail_api_jwt_revisions) AS revisions,
            (SELECT count(*) FROM retail_native_audit_outbox) AS events''')
    assert counts == [{"revisions": 2, "events": 2}]


@pytest.mark.asyncio
async def test_rotation_cannot_follow_a_deprecated_key_mapping(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('''CREATE TABLE "LiteLLM_DeprecatedVerificationToken"(
            token text PRIMARY KEY,new_token text,expires timestamp(3))''')
        await writer.query_raw('''INSERT INTO "LiteLLM_DeprecatedVerificationToken"(token,new_token,expires)
            VALUES($1,$2,now()+interval '1 hour') RETURNING token''', native.config.bindings[0].token_hash, "b" * 64)
        await writer.execute_raw('''UPDATE "LiteLLM_VerificationToken" SET token=$1''', "b" * 64)
    assert await native.store.fresh(native.config, native.token()) == ApiJwtFailure("invalid")

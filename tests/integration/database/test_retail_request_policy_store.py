import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import JsonValue, TypeAdapter

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import hash_token
from litellm.proxy.retail.native_audit_store import ensure_native_schema
from litellm.proxy.retail.request_policy_management import (
    BindingCreate,
    BindingUpdate,
    PolicyActor,
    PolicyCreate,
    PolicyUpdate,
    PromptCreate,
    PromptUpdate,
)
from litellm.proxy.retail.request_policy_store import NativeRequestPolicyStore, Reply, WriteInput
from litellm.proxy.retail.request_policy_types import Definition, RequestPolicyFailure, ResolvedRequestPolicies
from tests.integration.database.test_retail_scim_store import Writer

ADMIN_KEY: Final = hash_token("fixture-policy-admin")
CUSTOMER_KEY: Final = hash_token("fixture-policy-customer")
ACTOR: Final = PolicyActor("admin-owner", ADMIN_KEY)
MODEL: Final = "fixture-public-model"
PRIVATE: Final = "private-directory-prompt-data"
TEAM: Final = "fixture-native-team-one"


@dataclass(frozen=True, slots=True)
class DirectWriter:
    database_url: str

    async def query_raw(self, query: str, *args: object) -> object:
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
            return await Writer(connection).query_raw(query, *args)


@dataclass(frozen=True, slots=True)
class Client:
    database_url: str

    @property
    def writer_db(self) -> DirectWriter:
        return DirectWriter(self.database_url)

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[Writer]:
        async with asyncio.timeout(timeout.total_seconds()):
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                yield Writer(connection)


@dataclass(frozen=True, slots=True)
class TextDatetimeWriter:
    writer: Writer

    async def query_raw(self, query: str, *args: object) -> object:
        return await self.writer.query_raw(query, *(value.isoformat() if isinstance(value, datetime) else value for value in args))

    async def execute_raw(self, query: str, *args: object) -> int:
        return await self.writer.execute_raw(query, *args)


@dataclass(frozen=True, slots=True)
class TextDatetimeClient:
    client: Client

    @property
    def writer_db(self) -> DirectWriter:
        return self.client.writer_db

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[TextDatetimeWriter]:
        async with self.client.tx(timeout=timeout) as writer:
            yield TextDatetimeWriter(writer)


@dataclass(frozen=True, slots=True)
class TimestampWireWriter:
    writer: Writer

    async def query_raw(self, query: str, *args: object) -> object:
        if query.startswith("INSERT INTO retail_request_policy_targets("):
            for value in (args[4], args[6]):
                if value is not None and not isinstance(value, str):
                    raise TypeError("Target timestamps require stable text wire values")
        return await self.writer.query_raw(query, *args)

    async def execute_raw(self, query: str, *args: object) -> int:
        return await self.writer.execute_raw(query, *args)


@dataclass(frozen=True, slots=True)
class TimestampWireClient:
    client: Client

    @property
    def writer_db(self) -> DirectWriter:
        return self.client.writer_db

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[TimestampWireWriter]:
        async with self.client.tx(timeout=timeout) as writer:
            yield TimestampWireWriter(writer)


@dataclass(frozen=True, slots=True)
class Fixture:
    client: Client
    store: NativeRequestPolicyStore


@pytest_asyncio.fixture(loop_scope="function")
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    source: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not source:
        pytest.skip("A dedicated request policy PostgreSQL service is required")
    base: Final = conninfo_to_dict(source)
    name: Final = "retail_request_policy_"+uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    client: Final = Client(make_conninfo(**{**base, "dbname": name}))
    store: Final = NativeRequestPolicyStore(client, catalog=lambda: (MODEL, "second-public-model"))
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    try:
        async with client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.execute_raw('''CREATE TABLE "LiteLLM_UserTable"(
                user_id text PRIMARY KEY,user_role text,password text,password_reset_required boolean,
                created_at timestamp(3) DEFAULT now(),user_alias text,metadata jsonb DEFAULT '{}',
                models text[] DEFAULT '{}',spend float8 DEFAULT 0,max_budget float8);
                CREATE TABLE "LiteLLM_VerificationToken"(
                token text PRIMARY KEY,user_id text,models text[] DEFAULT '{}',blocked boolean,expires timestamp(3),
                created_at timestamp(3) DEFAULT now(),spend float8 DEFAULT 0,max_budget float8,
                key_alias text,team_id text,allowed_routes text[] DEFAULT '{}',metadata jsonb DEFAULT '{}');
                CREATE TABLE "LiteLLM_TeamTable"(
                team_id text PRIMARY KEY,team_alias text,created_at timestamp(3) NOT NULL DEFAULT now(),
                blocked boolean NOT NULL DEFAULT false,models text[] DEFAULT '{}',spend float8 DEFAULT 0,
                max_budget float8,max_parallel_requests integer,tpm_limit bigint,rpm_limit bigint,
                members_with_roles jsonb DEFAULT '[]',metadata jsonb DEFAULT '{}',model_id integer,object_permission_id text);
                CREATE TABLE "LiteLLM_ModelTable"(id integer PRIMARY KEY,aliases jsonb,created_by text,updated_by text);
                CREATE TABLE "LiteLLM_ObjectPermissionTable"(object_permission_id text PRIMARY KEY,models text[],mcp_servers text[]);
                CREATE TABLE "LiteLLM_BudgetTable"(budget_id text PRIMARY KEY,max_budget float8,allowed_models text[],
                    tpm_limit bigint,rpm_limit bigint,max_parallel_requests integer,created_by text,updated_by text);
                CREATE TABLE "LiteLLM_TeamMembership"(user_id text,team_id text,spend float8 DEFAULT 0,
                    total_spend float8 DEFAULT 0,budget_id text,PRIMARY KEY(user_id,team_id));
                CREATE TABLE retail_wallets(user_id text PRIMARY KEY,balance_nanos bigint);
                INSERT INTO retail_wallets VALUES('ordinary-owner',123456789);
                INSERT INTO "LiteLLM_UserTable"(user_id,user_role,password,user_alias) VALUES
                ('admin-owner','proxy_admin','private-admin-password','Operations'),
                ('ordinary-owner','customer','private-customer-password','Customer');''')
            await writer.query_raw('''INSERT INTO "LiteLLM_VerificationToken"(token,user_id,key_alias,max_budget,models)
                VALUES($1,'admin-owner','Admin session',100,ARRAY[$3]),
                ($2,'ordinary-owner','Client key',7,ARRAY[$3]) RETURNING token''', ADMIN_KEY, CUSTOMER_KEY, MODEL)
            await writer.query_raw('''INSERT INTO "LiteLLM_TeamTable"(team_id,team_alias,models,members_with_roles,max_budget)
                VALUES($1,'Primary fixture team',ARRAY[$2],'[{"user_id":"ordinary-owner","role":"user"}]',21),
                ('fixture-native-team-two','Secondary fixture team',ARRAY[$2],'[]',22) RETURNING team_id''', TEAM, MODEL)
        await ensure_native_schema(client)
        assert await store.initialize() is None
        yield Fixture(client, store)
    finally:
        await store.close()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def success(reply: Reply) -> Mapping[str, JsonValue]:
    assert not isinstance(reply, RequestPolicyFailure), reply
    return reply


async def created(native: Fixture, data: WriteInput, kind: str = "policies", *, key: str | None = None) -> Mapping[str, JsonValue]:
    assert kind in {"policies", "prompts", "bindings"}
    result: Final = await native.store.write(ACTOR, kind, "created", key or uuid4().hex, data)
    return success(result)


async def target(native: Fixture, kind: str, q: str = "") -> UUID:
    assert kind in {"model", "user", "key", "team"}
    reply: Final = success(await native.store.targets(ACTOR, kind, q=q))
    rows: Final = TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(reply["items"])
    assert rows
    return UUID(str(rows[0]["id"]))


async def policy(native: Fixture, *, prompt_id: UUID | None = None, enabled: bool = True) -> UUID:
    row: Final = await created(native, PolicyCreate(name="Bounded output", definition=Definition(max_output_tokens=256),
        prompt_id=prompt_id, enabled=enabled))
    return UUID(str(row["id"]))


async def assigned(native: Fixture, kind: str = "key", *, policy_id: UUID | None = None) -> Mapping[str, JsonValue]:
    selected: Final = await target(native, kind, "Client" if kind == "key" else "ordinary" if kind == "user" else "Primary" if kind == "team" else MODEL)
    return await created(native, BindingCreate(target_id=selected, policy_id=policy_id or await policy(native)), "bindings")


async def rows(native: Fixture, query: str, *args: object) -> object:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        return await writer.query_raw(query, *args)


@pytest.mark.asyncio
async def test_three_applicable_rules_fresh_identity_and_no_private_prompt_in_metadata(native: Fixture) -> None:
    prompt: Final = await created(native, PromptCreate(name="Editorial", content=PRIVATE), "prompts")
    identifier: Final = await policy(native, prompt_id=UUID(str(prompt["id"])))
    for kind in ("key", "user", "model"):
        await assigned(native, kind, policy_id=identifier)
    resolved: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(resolved, ResolvedRequestPolicies)
    assert tuple(item.target_kind for item in resolved.policies) == ("model", "user", "key")
    assert all(item.prompt_text == PRIVATE and item.definition.max_output_tokens == 256 for item in resolved.policies)
    assert resolved.user is not None and resolved.user.user_role == "customer"
    assert resolved.auth.user_id == "ordinary-owner" and resolved.auth.max_budget == 7
    assert resolved.auth.api_key == CUSTOMER_KEY and resolved.auth.token == CUSTOMER_KEY
    assert PRIVATE not in repr(resolved) and CUSTOMER_KEY not in repr(resolved)
    assert "password" not in resolved.user.model_fields_set
    listing: Final = success(await native.store.list_resources(ACTOR, "prompts"))
    assert PRIVATE not in json.dumps(listing) and "content" not in json.dumps(listing)
    single: Final = success(await native.store.get_resource(ACTOR, "prompts", UUID(str(prompt["id"]))))
    assert single["content"] == PRIVATE
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
async def test_target_responses_have_opaque_ids_and_server_catalog_only(native: Fixture) -> None:
    for kind in ("user", "key", "model", "team"):
        result: Final = success(await native.store.targets(ACTOR, kind))
        text: Final = json.dumps(result)
        assert CUSTOMER_KEY not in text and ADMIN_KEY not in text and "ordinary-owner" not in text
        assert "native_id" not in text and "owner_id" not in text and "anchor" not in text
        assert TEAM not in text
    models: Final = success(await native.store.targets(ACTOR, "model", q="not-public"))
    assert models["items"] == [] and models["total"] == 0
    page: Final = success(await native.store.targets(ACTOR, "model", limit=1, offset=1))
    assert page["total"] == 2 and len(TypeAdapter(list[JsonValue]).validate_python(page["items"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", (
    "UPDATE \"LiteLLM_UserTable\" SET user_role='customer' WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_UserTable\" SET metadata='{\"scim_active\":false}' WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_VerificationToken\" SET expires=now()-interval '1 day' WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_VerificationToken\" SET user_id='ordinary-owner' WHERE user_id='admin-owner'",
    "UPDATE \"LiteLLM_VerificationToken\" SET allowed_routes=ARRAY['llm_api_routes'] WHERE user_id='admin-owner'",
))
async def test_management_rechecks_admin_and_credential_in_current_native_transaction(native: Fixture, mutation: str) -> None:
    await rows(native, mutation+" RETURNING user_id")
    result: Final = await native.store.write(ACTOR, "policies", "created", "denied-write", PolicyCreate(name="No", definition=Definition()))
    assert result == RequestPolicyFailure("forbidden")
    assert await native.store.targets(ACTOR, "user") == RequestPolicyFailure("forbidden")
    assert await rows(native, "SELECT count(*) AS n FROM retail_request_policy_receipts") == [{"n": 0}]


@pytest.mark.asyncio
async def test_idempotency_replay_cas_and_delete_reference_constraints(native: Fixture) -> None:
    data: Final = PolicyCreate(name="Same", definition=Definition(temperature=0.5))
    simultaneous: Final = await asyncio.gather(*(native.store.write(ACTOR, "policies", "created", "same-operation", data) for _ in range(4)))
    first: Final = success(simultaneous[0])
    assert all(item == first for item in simultaneous)
    assert await native.store.write(ACTOR, "policies", "created", "same-operation", data.model_copy(update={"name": "Other"})) == RequestPolicyFailure("conflict")
    identifier: Final = UUID(str(first["id"]))
    update: Final = PolicyUpdate(name="Second", definition=Definition(), expected_version=1)
    updates: Final = await asyncio.gather(*(native.store.write(ACTOR, "policies", "updated", uuid4().hex, update, identifier, 1) for _ in range(2)))
    assert sum(isinstance(item, RequestPolicyFailure) and item.code == "conflict" for item in updates) == 1
    binding: Final = await assigned(native, policy_id=identifier)
    assert await native.store.write(ACTOR, "policies", "deleted", "referenced", None, identifier, 2) == RequestPolicyFailure("conflict")
    success(await native.store.write(ACTOR, "bindings", "deleted", "unassign", None, UUID(str(binding["id"])), 1))
    success(await native.store.write(ACTOR, "policies", "deleted", "delete-policy", None, identifier, 2))
    assert await native.store.get_resource(ACTOR, "policies", identifier) == RequestPolicyFailure("not_found")


@pytest.mark.asyncio
async def test_key_rotation_keeps_binding_and_rejects_owner_change_recreation_and_hash_reuse(native: Fixture) -> None:
    binding: Final = await assigned(native)
    replacement: Final = hash_token("fixture-rotated-policy-key")
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, replacement)
    resolved: Final = await native.store.lookup("ordinary-owner", replacement, MODEL)
    assert isinstance(resolved, ResolvedRequestPolicies) and len(resolved.policies) == 1
    assert resolved.policies[0].policy_id == UUID(str(binding["policy_id"]))
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")
    for mutation in ("user_id='admin-owner'", "created_at=created_at+interval '1 hour'"):
        with pytest.raises(psycopg.Error):
            await rows(native, f'UPDATE "LiteLLM_VerificationToken" SET {mutation} WHERE token=$1 RETURNING token', replacement)
    await rows(native, 'DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1 RETURNING token', replacement)
    with pytest.raises(psycopg.Error):
        await rows(native, 'INSERT INTO "LiteLLM_VerificationToken"(token,user_id) VALUES($1,\'ordinary-owner\') RETURNING token', replacement)
    success(await native.store.write(ACTOR, "bindings", "deleted", "unassign-deleted-key", None, UUID(str(binding["id"])), 1))
    await rows(native, 'INSERT INTO "LiteLLM_VerificationToken"(token,user_id) VALUES($1,\'ordinary-owner\') RETURNING token', replacement)
    after: Final = await native.store.lookup("ordinary-owner", replacement, MODEL)
    assert isinstance(after, ResolvedRequestPolicies) and not after.policies


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("user", "key"))
async def test_recreated_native_anchor_fails_closed_instead_of_silently_unassigning(native: Fixture, kind: str) -> None:
    await assigned(native, kind)
    await rows(native, 'UPDATE "LiteLLM_UserTable" SET created_at=created_at+interval \'1 hour\' WHERE user_id=\'ordinary-owner\' RETURNING user_id')
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", (
    "UPDATE retail_request_policy_policies SET enabled=false RETURNING id",
    "UPDATE retail_request_policy_prompts SET enabled=false RETURNING id",
    "UPDATE retail_request_policy_policies SET deleted=true RETURNING id",
    "UPDATE retail_request_policy_prompts SET deleted=true RETURNING id",
))
async def test_disabled_or_deleted_bound_policy_and_prompt_deny_until_explicit_unassign(native: Fixture, mutation: str) -> None:
    prompt: Final = await created(native, PromptCreate(name="Bound", content=PRIVATE), "prompts")
    identifier: Final = await policy(native, prompt_id=UUID(str(prompt["id"])))
    binding: Final = await assigned(native, policy_id=identifier)
    await rows(native, mutation)
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")
    data: Final = BindingUpdate(policy_id=identifier, enabled=False, expected_version=1)
    success(await native.store.write(ACTOR, "bindings", "updated", "disable-binding", data, UUID(str(binding["id"])), 1))
    resolved: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(resolved, ResolvedRequestPolicies) and not resolved.policies


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", (
    "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE user_id='ordinary-owner'",
    "UPDATE \"LiteLLM_VerificationToken\" SET expires=now()-interval '1 hour' WHERE user_id='ordinary-owner'",
    "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=true WHERE user_id='ordinary-owner'",
    "UPDATE \"LiteLLM_UserTable\" SET metadata='{\"scim_active\":false}' WHERE user_id='ordinary-owner'",
    "DELETE FROM \"LiteLLM_UserTable\" WHERE user_id='ordinary-owner'",
))
async def test_request_lookup_rejects_revocation_and_wrong_owner(native: Fixture, mutation: str) -> None:
    assert await native.store.lookup("admin-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")
    await rows(native, mutation+" RETURNING user_id")
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
async def test_master_without_native_row_and_ownerless_key_keep_model_rules(native: Fixture) -> None:
    await assigned(native, "model")
    master: Final = await native.store.lookup("master-without-row", "", MODEL, trusted_master=True)
    assert isinstance(master, ResolvedRequestPolicies) and master.user is None
    assert master.auth.user_id == "master-without-row" and master.auth.via_virtual_key is True
    assert tuple(item.target_kind for item in master.policies) == ("model",)
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET user_id=NULL WHERE token=$1 RETURNING token', CUSTOMER_KEY)
    ownerless: Final = await native.store.lookup(None, CUSTOMER_KEY, MODEL)
    assert isinstance(ownerless, ResolvedRequestPolicies) and ownerless.user is None
    assert tuple(item.target_kind for item in ownerless.policies) == ("model",)
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")
    master_actor: Final = PolicyActor(None, None, True)
    assert not isinstance(await native.store.list_resources(master_actor, "policies"), RequestPolicyFailure)


@pytest.mark.asyncio
async def test_prompt_reference_and_audit_receipts_contain_metadata_only(native: Fixture) -> None:
    prompt: Final = await created(native, PromptCreate(name="Private prompt", content=PRIVATE), "prompts")
    identifier: Final = UUID(str(prompt["id"]))
    await policy(native, prompt_id=identifier)
    assert await native.store.write(ACTOR, "prompts", "deleted", "delete-referenced", None, identifier, 1) == RequestPolicyFailure("conflict")
    update: Final = PromptUpdate(name="Private prompt", content=PRIVATE+" revised", expected_version=1)
    assert "content" not in success(await native.store.write(ACTOR, "prompts", "updated", "update-prompt", update, identifier, 1))
    audit: Final = await rows(native, "SELECT to_jsonb(a) AS value FROM retail_native_audit_outbox a")
    receipts: Final = await rows(native, "SELECT to_jsonb(r) AS value FROM retail_request_policy_receipts r")
    text: Final = json.dumps((audit, receipts), default=str)
    assert PRIVATE not in text and CUSTOMER_KEY not in text and ADMIN_KEY not in text
    assert '"native_request_policy.prompt_updated"' in text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,maximum", (("prompt", 1000), ("policy", 1000), ("target", 10000), ("binding", 10000), ("receipt", 10000)))
async def test_retained_caps_deny_without_partial_object_or_receipt(native: Fixture, kind: str, maximum: int) -> None:
    selected: Final = await target(native, "model", MODEL) if kind == "binding" else None
    identifier: Final = await policy(native) if kind == "binding" else None
    await rows(native, f"UPDATE retail_request_policy_settings SET {kind}_count=$1 RETURNING singleton", maximum)
    if kind == "target":
        result: Final = await native.store.targets(ACTOR, "user")
    else:
        data: Final = PromptCreate(name="Cap", content=PRIVATE) if kind == "prompt" else (
            BindingCreate(target_id=selected, policy_id=identifier) if selected and identifier else PolicyCreate(name="Cap", definition=Definition()))
        resource: Final = "prompts" if kind == "prompt" else "bindings" if kind == "binding" else "policies"
        result: Final = await native.store.write(ACTOR, resource, "created", "capped", data)
    assert result == RequestPolicyFailure("capacity")
    assert await rows(native, "SELECT count(*) AS n FROM retail_request_policy_receipts WHERE operation_key=$1", hashlib.sha256(b"capped").hexdigest()) == [{"n": 0}]


@pytest.mark.asyncio
async def test_schema_reinitialization_preserves_rows_and_key_rotation_behavior(native: Fixture) -> None:
    binding: Final = await assigned(native)
    before: Final = await rows(native, "SELECT * FROM retail_request_policy_settings")
    assert await native.store.initialize() is None
    assert await rows(native, "SELECT * FROM retail_request_policy_settings") == before
    selected: Final = success(await native.store.get_resource(ACTOR, "bindings", UUID(str(binding["id"]))))
    assert selected == binding
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
async def test_full_native_audit_queue_rolls_back_object_and_receipt_atomically(native: Fixture) -> None:
    await rows(native, "INSERT INTO retail_native_audit_settings(singleton,source_id,retained_count) VALUES(true,gen_random_uuid(),10000) RETURNING singleton")
    data: Final = PolicyCreate(name="Must rollback", definition=Definition())
    assert await native.store.write(ACTOR, "policies", "created", "queue-full", data) == RequestPolicyFailure("unavailable")
    assert await rows(native, "SELECT count(*) AS n FROM retail_request_policy_policies") == [{"n": 0}]
    assert await rows(native, "SELECT count(*) AS n FROM retail_request_policy_receipts") == [{"n": 0}]
    assert await rows(native, "SELECT policy_count,receipt_count FROM retail_request_policy_settings") == [{"policy_count": 0,"receipt_count": 0}]


@pytest.mark.asyncio
async def test_delete_recreate_reuses_no_old_key_binding_and_other_key_cannot_rotate_into_bound_tombstone(native: Fixture) -> None:
    await assigned(native)
    await rows(native, 'DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1 RETURNING token', CUSTOMER_KEY)
    with pytest.raises(psycopg.Error):
        await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1 RETURNING token', ADMIN_KEY, CUSTOMER_KEY)
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
async def test_runtime_sees_policy_edits_on_next_request_and_binding_no_longer_targets_deleted_catalog_model(native: Fixture) -> None:
    binding: Final = await assigned(native, "model")
    identifier: Final = UUID(str(binding["policy_id"]))
    data: Final = PolicyUpdate(name="Updated immediately", expected_version=1, definition=Definition(max_output_tokens=99))
    success(await native.store.write(ACTOR, "policies", "updated", "fresh-policy", data, identifier, 1))
    resolved: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(resolved, ResolvedRequestPolicies) and resolved.policies[0].definition.max_output_tokens == 99
    unavailable_catalog: Final = NativeRequestPolicyStore(native.client, catalog=lambda: ())
    change: Final = BindingUpdate(policy_id=identifier, enabled=True, expected_version=1)
    assert await unavailable_catalog.write(ACTOR, "bindings", "updated", "removed-model", change, UUID(str(binding["id"])), 1) == RequestPolicyFailure("forbidden")
    disable: Final = change.model_copy(update={"enabled": False})
    success(await unavailable_catalog.write(ACTOR, "bindings", "updated", "disable-removed-model", disable, UUID(str(binding["id"])), 1))


@pytest.mark.asyncio
async def test_native_rotation_and_new_assignment_serialize_without_losing_policy(native: Fixture) -> None:
    selected: Final = await target(native, "key", "Client")
    identifier: Final = await policy(native)
    replacement: Final = hash_token("fixture-race-rotated-key")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw('UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, replacement)
        task: Final = asyncio.create_task(native.store.write(ACTOR, "bindings", "created", "concurrent-binding",
            BindingCreate(target_id=selected, policy_id=identifier)))
        await asyncio.sleep(0)
    result: Final = await task
    assert not isinstance(result, RequestPolicyFailure), result
    current: Final = await native.store.lookup("ordinary-owner", replacement, MODEL)
    assert isinstance(current, ResolvedRequestPolicies) and len(current.policies) == 1
    assert current.policies[0].policy_id == identifier


@pytest.mark.asyncio
async def test_list_pages_are_stable_bounded_and_do_not_fetch_prompt_content(native: Fixture) -> None:
    for index in range(3):
        await created(native, PromptCreate(name=f"Prompt {index}", content=PRIVATE), "prompts")
    first: Final = success(await native.store.list_resources(ACTOR, "prompts", limit=2))
    second: Final = success(await native.store.list_resources(ACTOR, "prompts", limit=2, offset=2))
    page_one: Final = TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(first["items"])
    page_two: Final = TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(second["items"])
    assert first["total"] == second["total"] == 3
    assert len(page_one) == 2 and len(page_two) == 1
    assert not {item["id"] for item in page_one}.intersection(item["id"] for item in page_two)
    assert "content" not in json.dumps((first, second))
    assert await native.store.list_resources(ACTOR, "prompts", limit=101) == RequestPolicyFailure("invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("user", "key", "team"))
async def test_target_timestamps_accept_native_prisma_text_datetime_parameters(native: Fixture, kind: str) -> None:
    store: Final = NativeRequestPolicyStore(TextDatetimeClient(native.client), catalog=lambda: (MODEL,))
    reply: Final = success(await store.targets(ACTOR, kind))
    assert reply["total"] == 2
    assert len(TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(reply["items"])) == 2


@pytest.mark.asyncio
async def test_mixed_target_timestamp_shapes_use_stable_wire_values_and_preserve_anchors(native: Fixture) -> None:
    store: Final = NativeRequestPolicyStore(TimestampWireClient(native.client), catalog=lambda: (MODEL,))
    for kind in ("model", "team", "user", "key", "key", "user", "team", "model"):
        reply: Final = success(await store.targets(ACTOR, kind))
        items: Final = TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(reply["items"])
        current: Final = tuple(sorted(str(item["id"]) for item in items))
        assert len(current) == (1 if kind == "model" else 2)
        assert all(set(item) == {"id", "kind", "label"} for item in items)
        assert success(await store.targets(ACTOR, kind)) == reply
    shape: Final = await rows(native, '''SELECT kind,count(*) AS total,
        bool_and(native_created_at IS NULL) AS native_empty,
        bool_and(owner_created_at IS NULL) AS owner_empty,
        bool_and(owner_created_at IS NOT NULL) AS owner_present
        FROM retail_request_policy_targets GROUP BY kind ORDER BY kind''')
    assert shape == [
        {"kind": "key", "total": 2, "native_empty": False, "owner_empty": False, "owner_present": True},
        {"kind": "model", "total": 1, "native_empty": True, "owner_empty": True, "owner_present": False},
        {"kind": "team", "total": 2, "native_empty": False, "owner_empty": True, "owner_present": False},
        {"kind": "user", "total": 2, "native_empty": False, "owner_empty": True, "owner_present": False},
    ]
    assert await rows(native, "SELECT balance_nanos FROM retail_wallets") == [{"balance_nanos": 123456789}]


@pytest.mark.asyncio
async def test_four_scopes_use_current_key_team_and_a_single_fresh_authority_snapshot(native: Fixture) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    identifier: Final = await policy(native)
    for kind in ("key", "user", "team", "model"):
        await assigned(native, kind, policy_id=identifier)
    result: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(result, ResolvedRequestPolicies)
    assert tuple(rule.target_kind for rule in result.policies) == ("model", "team", "user", "key")
    assert result.auth.team_id == TEAM and result.team is not None and result.team.team_id == TEAM
    assert result.team.max_budget == 21
    assert TEAM not in repr(result)
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=\'fixture-native-team-two\' WHERE token=$1 RETURNING token', CUSTOMER_KEY)
    moved: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(moved, ResolvedRequestPolicies) and moved.team is not None
    assert moved.team.team_id == "fixture-native-team-two" and moved.team.max_budget == 22
    assert tuple(rule.target_kind for rule in moved.policies) == ("model", "user", "key")


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", (False, True))
@pytest.mark.parametrize("state", ("blocked", "deleted"))
async def test_missing_and_blocked_current_team_fail_closed_with_or_without_binding(native: Fixture, bound: bool, state: str) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    if bound:
        await assigned(native, "team")
    if state == "blocked":
        await rows(native, 'UPDATE "LiteLLM_TeamTable" SET blocked=true WHERE team_id=$1 RETURNING team_id', TEAM)
    else:
        await rows(native, 'DELETE FROM "LiteLLM_TeamTable" WHERE team_id=$1 RETURNING team_id', TEAM)
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
async def test_bound_team_identity_and_deleted_identifier_cannot_be_reused_until_explicit_unassignment(native: Fixture) -> None:
    binding: Final = await assigned(native, "team")
    for change in ("created_at=created_at+interval '1 hour'", "team_id='attempted-rename'"):
        with pytest.raises(psycopg.Error):
            await rows(native, f'UPDATE "LiteLLM_TeamTable" SET {change} WHERE team_id=$1 RETURNING team_id', TEAM)
    await rows(native, 'DELETE FROM "LiteLLM_TeamTable" WHERE team_id=$1 RETURNING team_id', TEAM)
    with pytest.raises(psycopg.Error):
        await rows(native, 'INSERT INTO "LiteLLM_TeamTable"(team_id,team_alias) VALUES($1,\'Recreated team\') RETURNING team_id', TEAM)
    success(await native.store.write(ACTOR, "bindings", "deleted", "unassign-retired-team", None, UUID(str(binding["id"])), 1))
    await rows(native, 'INSERT INTO "LiteLLM_TeamTable"(team_id,team_alias) VALUES($1,\'Recreated team\') RETURNING team_id', TEAM)
    fresh_target: Final = await target(native, "team", "Recreated")
    assert str(fresh_target) != binding["target_id"]


@pytest.mark.asyncio
async def test_tampered_team_anchor_cannot_silently_drop_its_binding(native: Fixture) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    await assigned(native, "team")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('ALTER TABLE "LiteLLM_TeamTable" DISABLE TRIGGER retail_request_policy_team_guard')
        await writer.query_raw('UPDATE "LiteLLM_TeamTable" SET created_at=created_at+interval \'1 hour\' WHERE team_id=$1 RETURNING team_id', TEAM)
        await writer.execute_raw('ALTER TABLE "LiteLLM_TeamTable" ENABLE TRIGGER retail_request_policy_team_guard')
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
async def test_team_related_alias_permission_and_member_budget_are_fresh_and_cleared_after_key_moves(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw('''INSERT INTO "LiteLLM_ModelTable"(id,aliases,created_by,updated_by)
            VALUES(1,'{"visible-alias":"fixture-public-model"}','fixture','fixture') RETURNING id''')
        await writer.query_raw('''INSERT INTO "LiteLLM_ObjectPermissionTable"(object_permission_id,models,mcp_servers)
            VALUES('fixture-permissions',ARRAY[$1],ARRAY['fixture-mcp']) RETURNING object_permission_id''', MODEL)
        await writer.query_raw('''INSERT INTO "LiteLLM_BudgetTable"(budget_id,max_budget,allowed_models,tpm_limit,rpm_limit)
            VALUES('fixture-member-budget',4,ARRAY[$1],17,3) RETURNING budget_id''', MODEL)
        await writer.query_raw('''INSERT INTO "LiteLLM_TeamMembership"(user_id,team_id,budget_id)
            VALUES('ordinary-owner',$1,'fixture-member-budget') RETURNING user_id''', TEAM)
        await writer.query_raw('''UPDATE "LiteLLM_TeamTable" SET model_id=1,object_permission_id='fixture-permissions'
            WHERE team_id=$1 RETURNING team_id''', TEAM)
        await writer.query_raw('UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    result: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(result, ResolvedRequestPolicies) and result.team is not None
    assert result.team.litellm_model_table is not None and result.team.litellm_model_table.model_aliases == {"visible-alias": MODEL}
    assert result.team.object_permission is not None and result.team.object_permission.mcp_servers == ["fixture-mcp"]
    assert result.team_membership is not None and result.team_membership.litellm_budget_table is not None
    assert result.team_membership.litellm_budget_table.max_budget == 4
    assert result.team_membership.litellm_budget_table.rpm_limit == 3
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=NULL WHERE token=$1 RETURNING token', CUSTOMER_KEY)
    moved: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(moved, ResolvedRequestPolicies) and moved.team is None and moved.team_membership is None


@pytest.mark.asyncio
async def test_r15_six_table_schema_upgrade_preserves_existing_targets_bindings_receipts_and_wallets(native: Fixture) -> None:
    binding: Final = await assigned(native)
    before: Final = await rows(native, '''SELECT
        (SELECT count(*) FROM retail_request_policy_receipts) AS receipts,
        (SELECT count(*) FROM retail_request_policy_targets) AS targets,
        (SELECT count(*) FROM retail_request_policy_bindings) AS bindings''')
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('''ALTER TABLE retail_request_policy_targets DROP CONSTRAINT retail_request_policy_targets_kind_v2_check;
            ALTER TABLE retail_request_policy_targets ADD CONSTRAINT retail_request_policy_targets_kind_check CHECK(kind IN('user','key','model'))''')
    assert await native.store.initialize() is None
    assert await rows(native, '''SELECT
        (SELECT count(*) FROM retail_request_policy_receipts) AS receipts,
        (SELECT count(*) FROM retail_request_policy_targets) AS targets,
        (SELECT count(*) FROM retail_request_policy_bindings) AS bindings''') == before
    assert success(await native.store.get_resource(ACTOR, "bindings", UUID(str(binding["id"])))) == binding
    team_binding: Final = await assigned(native, "team")
    assert team_binding["target_kind"] == "team"
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded_metadata", (False, True))
async def test_default_member_budget_is_part_of_same_team_snapshot_and_does_not_return_after_deletion(native: Fixture, encoded_metadata: bool) -> None:
    await rows(native, '''INSERT INTO "LiteLLM_BudgetTable"(budget_id,max_budget,allowed_models,rpm_limit)
        VALUES('default-member',3,ARRAY[$1],2) RETURNING budget_id''', MODEL)
    metadata: Final = {"team_member_budget_id": "default-member"}
    payload: Final = json.dumps(json.dumps(metadata)) if encoded_metadata else json.dumps(metadata)
    await rows(native, 'UPDATE "LiteLLM_TeamTable" SET metadata=$2::jsonb WHERE team_id=$1 RETURNING team_id', TEAM, payload)
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    result: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(result, ResolvedRequestPolicies) and result.team_member_default_budget is not None
    assert result.team_member_default_budget.rpm_limit == 2 and result.team_member_default_budget.max_budget == 3
    await rows(native, 'DELETE FROM "LiteLLM_BudgetTable" WHERE budget_id=\'default-member\' RETURNING budget_id')
    removed: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(removed, ResolvedRequestPolicies) and removed.team_member_default_budget is None


@pytest.mark.asyncio
async def test_reserved_ui_team_is_never_an_entity_target_and_preserves_native_session_scope(native: Fixture) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, UI_SESSION_TOKEN_TEAM_ID)
    session: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
    assert isinstance(session, ResolvedRequestPolicies) and session.auth.team_id == UI_SESSION_TOKEN_TEAM_ID
    assert session.team is None and session.team_membership is None
    await rows(native, 'INSERT INTO "LiteLLM_TeamTable"(team_id,team_alias,blocked) VALUES($1,\'Reserved UI fixture\',true) RETURNING team_id', UI_SESSION_TOKEN_TEAM_ID)
    listed: Final = success(await native.store.targets(ACTOR, "team", q="Reserved UI"))
    assert listed["items"] == [] and listed["total"] == 0
    identifier: Final = uuid4()
    await rows(native, '''INSERT INTO retail_request_policy_targets(id,kind,native_id,anchor,native_created_at,label)
        SELECT $1::uuid,'team',team_id,$2,created_at AT TIME ZONE 'UTC','Reserved' FROM "LiteLLM_TeamTable"
        WHERE team_id=$3 RETURNING id''', str(identifier), "a"*64, UI_SESSION_TOKEN_TEAM_ID)
    assigned_policy: Final = await policy(native)
    assert await native.store.write(ACTOR, "bindings", "created", "bind-reserved-team",
        BindingCreate(target_id=identifier, policy_id=assigned_policy)) == RequestPolicyFailure("forbidden")


@pytest.mark.asyncio
async def test_native_administrator_verification_is_fresh_read_only_and_creates_no_audit_receipts(native: Fixture) -> None:
    assert await native.store.verify_administrator(ACTOR) is None
    assert await native.store.verify_administrator(PolicyActor(None, None, True)) is None
    await rows(native, 'UPDATE "LiteLLM_UserTable" SET user_role=\'customer\' WHERE user_id=\'admin-owner\' RETURNING user_id')
    assert await native.store.verify_administrator(ACTOR) == RequestPolicyFailure("forbidden")
    assert await rows(native, "SELECT count(*) AS total FROM retail_request_policy_receipts") == [{"total": 0}]
    assert await rows(native, "SELECT count(*) AS total FROM retail_native_audit_outbox") == [{"total": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ("blocked", "deleted"))
async def test_concurrent_team_lifecycle_reads_one_committed_snapshot_and_rejects_following_requests(native: Fixture, transition: str) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    await assigned(native, "team")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        if transition == "blocked":
            await writer.query_raw('UPDATE "LiteLLM_TeamTable" SET blocked=true WHERE team_id=$1 RETURNING team_id', TEAM)
        else:
            await writer.query_raw('DELETE FROM "LiteLLM_TeamTable" WHERE team_id=$1 RETURNING team_id', TEAM)
        concurrent: Final = await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL)
        assert isinstance(concurrent, ResolvedRequestPolicies) and concurrent.team is not None
        assert concurrent.team.blocked is False and tuple(rule.target_kind for rule in concurrent.policies) == ("team",)
    assert await native.store.lookup("ordinary-owner", CUSTOMER_KEY, MODEL) == RequestPolicyFailure("forbidden")

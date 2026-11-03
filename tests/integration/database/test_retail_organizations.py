import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import JsonValue, TypeAdapter

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.control_plane_access import (
    ControlPlaneActor,
    PrismaIdentityReader,
    active_target_users,
    verify_actor,
)
from litellm.proxy.retail.models import IdempotencyConflict
from litellm.proxy.retail.operation_events import initialize_schema as initialize_events
from litellm.proxy.retail.organization_store import (
    MemberAssociate,
    MemberUpdate,
    OrganizationCreate,
    OrganizationStore,
    OrganizationUpdate,
)
from litellm.proxy.retail.organization_store import (
    initialize_schema as initialize_organizations,
)
from litellm.proxy.retail.store import WalletStore

ADMIN: Final = ControlPlaneActor("proxy-admin", "proxy_admin", True, True)
DELEGATE: Final = ControlPlaneActor("delegate", "internal_user", False, True)
MEMBER: Final = ControlPlaneActor("member", "internal_user", False, True)
FOREIGN: Final = ControlPlaneActor("foreign", "internal_user", False, True)
NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DICT: Final = TypeAdapter(dict[str, JsonValue])
_ITEMS: Final = TypeAdapter(list[dict[str, JsonValue]])


@dataclass(frozen=True, slots=True)
class NativeWriter:
    database_url: str

    async def query_raw(self, query: str, *args: object) -> object:
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
            return await (await connection.execute(query.replace("$1", "%s"), args)).fetchall()


@dataclass(frozen=True, slots=True)
class OrganizationRuntime:
    store: WalletStore
    native_url: str
    organizations: OrganizationStore
    reader: PrismaIdentityReader


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OrganizationRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated organization-test PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "true")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_org_" + uuid4().hex, "retail_authority_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    retail_url: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    store: Final = WalletStore(retail_url, max_connections=8, diagnostic_connections=3)
    reader: Final = PrismaIdentityReader(NativeWriter(native_url))
    try:
        async with await psycopg.AsyncConnection.connect(retail_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with await psycopg.AsyncConnection.connect(native_url) as connection:
            await connection.execute(
                """CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY,user_role TEXT,
                password_reset_required BOOLEAN DEFAULT false)"""
            )
            await connection.execute(
                """CREATE TABLE "LiteLLM_VerificationToken"(token TEXT PRIMARY KEY,user_id TEXT,team_id TEXT,
                blocked BOOLEAN DEFAULT false,expires TIMESTAMPTZ,allowed_routes TEXT[] DEFAULT '{}')"""
            )
            for user_id in ("proxy-admin", "delegate", "member", "foreign", "other-admin"):
                await connection.execute(
                    'INSERT INTO "LiteLLM_UserTable"(user_id,user_role) VALUES(%s,%s)',
                    (user_id, "proxy_admin" if user_id == "proxy-admin" else "internal_user"),
                )
        await store.initialize()
        async with store.transaction() as connection:
            await initialize_events(connection)
            await initialize_organizations(connection)
        yield OrganizationRuntime(store, native_url, OrganizationStore(store, identity_reader=reader), reader)
    finally:
        await store.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _organization(runtime: OrganizationRuntime) -> Mapping[str, JsonValue]:
    return await runtime.organizations.create(
        OrganizationCreate(name="Example organization", admin_user_id=DELEGATE.user_id), ADMIN, "create"
    )


def _id(organization: Mapping[str, JsonValue]) -> str:
    value: Final = organization["id"]
    assert isinstance(value, str)
    return value


async def _member(runtime: OrganizationRuntime, organization_id: str, version: int = 1) -> Mapping[str, JsonValue]:
    return await runtime.organizations.associate(
        organization_id,
        MemberAssociate(user_id=MEMBER.user_id, role="member", expected_organization_version=version),
        ADMIN,
        "associate-member",
    )


@pytest.mark.asyncio
async def test_concurrent_retries_create_one_organization_and_one_durable_audit(runtime: OrganizationRuntime) -> None:
    payload: Final = OrganizationCreate(name="One organization", admin_user_id=DELEGATE.user_id)
    results: Final = await asyncio.gather(*(runtime.organizations.create(payload, ADMIN, "retry") for _ in range(8)))
    assert all(result == results[0] for result in results)
    async with runtime.store.transaction() as connection:
        counts: Final = await (
            await connection.execute(
                """SELECT (SELECT count(*) FROM retail_organizations) AS organizations,
                (SELECT count(*) FROM retail_organization_memberships) AS members,
                (SELECT count(*) FROM retail_operation_events) AS events"""
            )
        ).fetchone()
    assert counts == {"organizations": 1, "members": 1, "events": 1}
    with pytest.raises(IdempotencyConflict):
        await runtime.organizations.create(payload.model_copy(update={"name": "Changed"}), ADMIN, "retry")
    assert (await runtime.organizations.list(ADMIN, admin=True))["total"] == 1


@pytest.mark.asyncio
async def test_ordinary_members_get_own_basic_info_without_roster_or_foreign_orgs(runtime: OrganizationRuntime) -> None:
    organization_id: Final = _id(await _organization(runtime))
    await _member(runtime, organization_id)
    own: Final = await runtime.organizations.get(organization_id, MEMBER)
    assert (own["member_role"], own["can_manage_members"], own["can_view_events"]) == ("member", False, False)
    with pytest.raises(HTTPException) as roster:
        await runtime.organizations.members(organization_id, MEMBER)
    with pytest.raises(HTTPException) as foreign:
        await runtime.organizations.get(organization_id, FOREIGN)
    assert (roster.value.status_code, foreign.value.status_code) == (403, 404)
    assert (await runtime.organizations.list(FOREIGN))["items"] == []


@pytest.mark.asyncio
async def test_delegated_admin_only_toggles_already_approved_ordinary_members(runtime: OrganizationRuntime) -> None:
    organization_id: Final = _id(await _organization(runtime))
    await _member(runtime, organization_id)
    with pytest.raises(HTTPException) as association:
        await runtime.organizations.associate(
            organization_id,
            MemberAssociate(user_id="unknown-native-account", role="member", expected_organization_version=2),
            DELEGATE,
            "unauthorized-association",
        )
    with pytest.raises(HTTPException) as promotion:
        await runtime.organizations.update_member(
            organization_id,
            MEMBER.user_id,
            MemberUpdate(role="admin", enabled=True, expected_version=1, expected_organization_version=2),
            DELEGATE,
            "unauthorized-promotion",
        )
    assert (association.value.status_code, promotion.value.status_code) == (403, 403)
    changed: Final = await runtime.organizations.update_member(
        organization_id,
        MEMBER.user_id,
        MemberUpdate(role="member", enabled=False, expected_version=1, expected_organization_version=2),
        DELEGATE,
        "disable-member",
    )
    assert _DICT.validate_python(changed["member"])["enabled"] is False
    assert (await runtime.organizations.list(MEMBER))["total"] == 0
    with pytest.raises(HTTPException) as revoked:
        await OrganizationStore(runtime.store, identity_reader=runtime.reader).get(organization_id, MEMBER)
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
async def test_concurrent_cas_allows_one_change_without_duplicate_or_misleading_events(
    runtime: OrganizationRuntime,
) -> None:
    organization_id: Final = _id(await _organization(runtime))
    outcomes: Final = await asyncio.gather(
        runtime.organizations.update(
            organization_id, OrganizationUpdate(name="First", enabled=True, expected_version=1), ADMIN, "first"
        ),
        runtime.organizations.update(
            organization_id, OrganizationUpdate(name="Second", enabled=True, expected_version=1), ADMIN, "second"
        ),
        return_exceptions=True,
    )
    successes: Final = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts: Final = [outcome.status_code for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(successes) == 1 and conflicts == [409]
    assert (await runtime.organizations.get(organization_id, ADMIN))["version"] == 2
    assert (await runtime.organizations.events(organization_id, ADMIN))["total"] == 2


@pytest.mark.asyncio
async def test_last_approved_admin_cannot_be_disabled(runtime: OrganizationRuntime) -> None:
    organization_id: Final = _id(await _organization(runtime))
    with pytest.raises(HTTPException) as rejected:
        await runtime.organizations.update_member(
            organization_id,
            DELEGATE.user_id,
            MemberUpdate(role="member", enabled=False, expected_version=1, expected_organization_version=1),
            ADMIN,
            "remove-last-admin",
        )
    assert rejected.value.status_code == 409
    roster: Final = await runtime.organizations.members(organization_id, ADMIN)
    assert [(item["role"], item["enabled"]) for item in _ITEMS.validate_python(roster["items"])] == [("admin", True)]
    assert (await runtime.organizations.events(organization_id, ADMIN))["total"] == 1


@pytest.mark.asyncio
async def test_disabling_org_immediately_hides_it_and_replayed_delegated_changes(runtime: OrganizationRuntime) -> None:
    organization_id: Final = _id(await _organization(runtime))
    await _member(runtime, organization_id)
    change: Final = MemberUpdate(role="member", enabled=False, expected_version=1, expected_organization_version=2)
    await runtime.organizations.update_member(organization_id, MEMBER.user_id, change, DELEGATE, "old-authority")
    await runtime.organizations.update(
        organization_id, OrganizationUpdate(name="Disabled", enabled=False, expected_version=3), ADMIN, "disable-org"
    )
    with pytest.raises(HTTPException) as replay:
        await runtime.organizations.update_member(organization_id, MEMBER.user_id, change, DELEGATE, "old-authority")
    assert replay.value.status_code == 404
    assert (await runtime.organizations.list(DELEGATE))["items"] == []
    assert (await runtime.organizations.get(organization_id, ADMIN))["enabled"] is False


@pytest.mark.asyncio
async def test_delegation_flag_revokes_admin_actions_without_changing_membership(
    runtime: OrganizationRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization_id: Final = _id(await _organization(runtime))
    assert (await runtime.organizations.get(organization_id, DELEGATE))["can_manage_members"] is True
    monkeypatch.setenv("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "false")
    assert (await runtime.organizations.get(organization_id, DELEGATE))["can_manage_members"] is False
    with pytest.raises(HTTPException) as denied:
        await runtime.organizations.members(organization_id, DELEGATE)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_audit_insert_failure_rolls_back_organization_change(runtime: OrganizationRuntime) -> None:
    async with runtime.store.transaction() as connection:
        await connection.execute(
            """ALTER TABLE retail_operation_events ADD CONSTRAINT reject_fixture_event
            CHECK (action <> 'organization.created')"""
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        await _organization(runtime)
    async with runtime.store.transaction() as connection:
        row: Final = await (
            await connection.execute(
                """SELECT (SELECT count(*) FROM retail_organizations) AS organizations,
                (SELECT count(*) FROM retail_organization_memberships) AS members"""
            )
        ).fetchone()
    assert row == {"organizations": 0, "members": 0}


@pytest.mark.asyncio
async def test_audit_is_append_only_and_public_projection_omits_private_replay_data(
    runtime: OrganizationRuntime,
) -> None:
    organization_id: Final = _id(await _organization(runtime))
    for statement in ("UPDATE retail_operation_events SET actor_id='changed'", "DELETE FROM retail_operation_events"):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            async with runtime.store.transaction() as connection:
                await connection.execute(statement)
    page: Final = await runtime.organizations.events(organization_id, DELEGATE)
    event: Final = _ITEMS.validate_python(page["items"])[0]
    assert set(event) == {
        "id",
        "organization_id",
        "actor_id",
        "action",
        "resource_type",
        "resource_id",
        "result",
        "version_before",
        "version_after",
        "changes",
        "created_at",
    }
    assert event["changes"] == {"name": "Example organization", "user_id": "delegate", "role": "admin"}
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_organization_changes_do_not_modify_wallet_ownership_or_financial_history(
    runtime: OrganizationRuntime,
) -> None:
    await runtime.store.get_wallet(MEMBER.user_id, create=True)
    await runtime.store.credit(MEMBER.user_id, "2", "fixture-credit", "fixture", "fixture", ADMIN.user_id)
    before: Final = await runtime.store.get_wallet(MEMBER.user_id)
    organization_id: Final = _id(await _organization(runtime))
    await _member(runtime, organization_id)
    await runtime.organizations.update_member(
        organization_id,
        MEMBER.user_id,
        MemberUpdate(role="member", enabled=False, expected_version=1, expected_organization_version=2),
        DELEGATE,
        "disable",
    )
    assert await runtime.store.get_wallet(MEMBER.user_id) == before
    async with runtime.store.transaction() as connection:
        ledger: Final = await (
            await connection.execute("SELECT user_id,kind,amount_nanos FROM retail_ledger")
        ).fetchall()
    assert ledger == [{"user_id": MEMBER.user_id, "kind": "credit", "amount_nanos": 2000000000}]


@pytest.mark.asyncio
async def test_ui_session_authority_uses_separate_native_writer_and_observes_demotion_then_revocation(
    runtime: OrganizationRuntime,
) -> None:
    auth: Final = UserAPIKeyAuth(
        user_id=ADMIN.user_id,
        api_key="sk-ui-session-fixture",
        user_role=LitellmUserRoles.PROXY_ADMIN,
        team_id="litellm-dashboard",
    )
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(
            'INSERT INTO "LiteLLM_VerificationToken"(token,user_id) VALUES(%s,%s)', (auth.api_key, ADMIN.user_id)
        )
    initial: Final = await verify_actor(auth, runtime.store, reader=runtime.reader, now=NOW)
    assert (initial.is_admin, initial.can_write) == (True, True)
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute(
            """UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin_viewer' WHERE user_id=%s""", (ADMIN.user_id,)
        )
    demoted: Final = await verify_actor(auth, runtime.store, reader=runtime.reader, now=NOW)
    assert (demoted.is_admin, demoted.can_write) == (False, False)
    with pytest.raises(HTTPException) as mutation:
        await runtime.organizations.create(
            OrganizationCreate(name="Denied", admin_user_id=DELEGATE.user_id), demoted, "denied"
        )
    assert mutation.value.status_code == 403
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token=%s', (auth.api_key,))
    with pytest.raises(HTTPException) as revoked:
        await verify_actor(auth, runtime.store, reader=runtime.reader, now=NOW)
    assert revoked.value.status_code == 403
    assert await active_target_users((MEMBER.user_id, "missing"), reader=runtime.reader) == frozenset((MEMBER.user_id,))


@pytest.mark.asyncio
async def test_saturated_management_connections_do_not_block_wallet_settlement(runtime: OrganizationRuntime) -> None:
    await runtime.store.get_wallet(MEMBER.user_id, create=True)
    await runtime.store.credit(MEMBER.user_id, "1", "credit", "fixture", "fixture", ADMIN.user_id)
    await runtime.store.reserve(MEMBER.user_id, "request", "fixture-model", "0.1", {"version": "fixture"})
    async with AsyncExitStack() as occupied:
        for _ in range(3):
            await occupied.enter_async_context(runtime.store.diagnostic_transaction())
        async with asyncio.timeout(2):
            settled: Final = await runtime.store.settle("request", "0.02", {"usage": {"input_tokens": 10}})
        assert settled["status"] == "settled"
        wallet: Final = await runtime.store.get_wallet(MEMBER.user_id)
        assert (wallet["balance"], wallet["reserved"]) == ("0.980000000", "0.000000000")
        assert runtime.store.pool_stats()["diagnostic"]["pool_available"] == 0

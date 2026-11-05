from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request

import litellm
from litellm.proxy import proxy_server
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.retail import coordination_guard, scim_identity
from litellm.proxy.retail.billing import RetailBilling
from litellm.proxy.retail.coordination_guard import CoordinationMonitor, GuardConfig
from litellm.proxy.retail.scim_config import ScimSetup
from litellm.proxy.retail.scim_groups_protocol import GroupProfile, GroupRecord
from tests.integration.database.test_retail_scim_store import (
    Client,
    Fixture,
    changed,
    created,
    issued,
    native,  # noqa: F401  # pytest registers the imported PostgreSQL fixture for this test module
    patch,
)


@dataclass(frozen=True, slots=True)
class NativeClient:
    source: Client
    db: object = field(default_factory=object)

    def tx(self, *, timeout: timedelta) -> object:
        return self.source.tx(timeout=timeout)


async def _healthy() -> bool:
    return True


async def _recover() -> None:
    return None


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def native_runtime(native: Fixture, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monitor: Final = CoordinationMonitor(GuardConfig(required=True), probe=_healthy, recover=_recover)
    await monitor.check_once()
    await monitor.start()
    monkeypatch.setattr(coordination_guard._lifecycle, "monitor", monitor)
    monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(native.client))
    monkeypatch.setattr(proxy_server, "master_key", "sk-fixture-master")
    monkeypatch.setattr(proxy_server, "general_settings", {})
    monkeypatch.setattr(proxy_server, "user_custom_auth", None)
    monkeypatch.setattr(litellm, "callbacks", [RetailBilling()])
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    monkeypatch.delenv("DATABASE_URL_READ_REPLICA", raising=False)
    try:
        yield
    finally:
        await monitor.close()


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})


@pytest.mark.asyncio
async def test_real_writer_guard_keeps_native_limits_without_caching_password_or_directory_profile(native: Fixture) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('ALTER TABLE "LiteLLM_UserTable" ADD COLUMN max_budget float8,ADD COLUMN rpm_limit bigint')
        await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET password=$2,max_budget=7,rpm_limit=17 WHERE user_id=$1', account.user_id, "fixture-private-native-password-hash")
    selected: Final = request()
    resolved: Final = await scim_identity.resolve_scim_identity(selected, UserAPIKeyAuth(user_id=account.user_id, user_role="proxy_admin"), token)
    assert resolved is not None
    assert resolved.auth.user_role == "customer" and resolved.auth.models == ["fixture-model"]
    assert resolved.user.max_budget == 7 and resolved.user.rpm_limit == 17 and resolved.user.password is None
    assert resolved.auth.api_key == token and resolved.auth.token == token
    assert account.external_id not in repr(selected.scope) and account.user_id not in repr(selected.scope)
    scim_identity.require_scim_identity_result(selected, resolved.auth)


@pytest.mark.asyncio
async def test_both_suspend_and_restore_revoke_old_proofs_even_when_scim_flag_removed(native: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    account: Final = await created(native)
    old: Final = await issued(native, account)
    suspended: Final = await changed(native, account, patch({"op": "replace", "path": "active", "value": False}))
    with pytest.raises(HTTPException) as inactive:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), old)
    assert inactive.value.status_code == 401
    restored: Final = await changed(native, suspended, patch({"op": "replace", "path": "active", "value": True}))
    monkeypatch.setenv("RETAIL_SCIM_ENABLED", "false")
    monkeypatch.delenv("RETAIL_SCIM_CONFIG", raising=False)
    with pytest.raises(HTTPException) as revoked:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), old)
    assert revoked.value.status_code == 401
    fresh: Final = await issued(native, restored, "fresh")
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), fresh) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ("blocked", "expired", "password-reset", "missing-key", "wrong-role", "missing-user"))
async def test_guard_checks_live_native_state_instead_of_cached_authority(native: Fixture, condition: str) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        if condition == "blocked":
            await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token=$1', token)
        elif condition == "expired":
            await writer.execute_raw('''UPDATE "LiteLLM_VerificationToken" SET expires=(clock_timestamp() AT TIME ZONE 'UTC')-interval '1 second' WHERE token=$1''', token)
        elif condition == "password-reset":
            await writer.execute_raw('UPDATE "LiteLLM_UserTable" SET password_reset_required=true WHERE user_id=$1', account.user_id)
        elif condition == "missing-key":
            await writer.execute_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1', token)
        else:
            await writer.execute_raw('ALTER TABLE "LiteLLM_UserTable" DISABLE TRIGGER retail_scim_native_user_guard')
            if condition == "wrong-role":
                await writer.execute_raw('''UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin' WHERE user_id=$1''', account.user_id)
            else:
                await writer.execute_raw('DELETE FROM "LiteLLM_UserTable" WHERE user_id=$1', account.user_id)
    with pytest.raises(HTTPException) as failure:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id, user_role="customer"), token)
    assert failure.value.status_code == 401 and token not in str(failure.value)


@pytest.mark.asyncio
async def test_rotation_only_accepts_current_exact_hash_and_retains_revoked_hash_proof(native: Fixture) -> None:
    account: Final = await created(native)
    original: Final = await issued(native, account)
    replacement: Final = "b" * 64
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET token=$2 WHERE token=$1', original, replacement)
        proofs: Final = await writer.query_raw('SELECT token_hash FROM retail_scim_credentials WHERE user_id=$1 ORDER BY token_hash', account.user_id)
    assert len(proofs) == 2
    with pytest.raises(HTTPException):
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id, token=replacement), original)
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), replacement) is not None


@pytest.mark.asyncio
async def test_deleted_resource_and_missing_schema_fail_closed_but_ordinary_identity_does_not_query(native: Fixture) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    assert await native.store.delete_user(native.setup, account.id, None) is None
    with pytest.raises(HTTPException) as deleted:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    assert deleted.value.status_code == 401
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw('DROP TABLE retail_scim_credentials CASCADE')
    with pytest.raises(HTTPException) as unavailable:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    assert unavailable.value.status_code == 503
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id="ordinary-owner"), "sk-fixture-ordinary") is None


@pytest.mark.asyncio
async def test_oidc_mint_directory_check_and_session_proof_check_share_writer_authority(native: Fixture) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await scim_identity.require_scim_directory_user(writer, account.user_id, lock=True)
        await scim_identity.require_scim_session(writer, account.user_id, token)
    suspended: Final = await changed(native, account, patch({"op": "replace", "path": "active", "value": False}))
    assert suspended.active is False
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        with pytest.raises(HTTPException):
            await scim_identity.require_scim_directory_user(writer, account.user_id, lock=True)
        with pytest.raises(HTTPException):
            await scim_identity.require_scim_session(writer, account.user_id, token)


async def access_list(native: Fixture, *external_ids: str) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw(
            "UPDATE retail_scim_settings SET access_group_external_ids=$1::text[] WHERE singleton", list(external_ids),
        )


async def directory_group(
    native: Fixture, resource_id: UUID, *, namespace: str | None = None, external_id: str = "allowed-external",
    removed: bool = False, deleted: bool = False,
) -> UUID:
    group_id: Final = uuid4()
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw(
            """INSERT INTO retail_scim_groups(id,namespace,external_id,display_name,deleted)
            VALUES($1::uuid,$2,$3,'Identity fixture',$4)""",
            str(group_id), namespace or native.setup.config.namespace, external_id, deleted,
        )
        await writer.execute_raw(
            """INSERT INTO retail_scim_group_members(group_id,user_resource_id,removed)
            VALUES($1::uuid,$2::uuid,$3)""", str(group_id), str(resource_id), removed,
        )
    return group_id


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", (
    "no-group", "removed-member", "deleted-group", "other-namespace", "other-user", "unlisted-group",
    "missing-settings", "wrong-settings-namespace",
))
async def test_current_group_authority_denies_key_directory_and_session_despite_valid_proof(
    native: Fixture, condition: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    await access_list(native, "allowed-external")
    if condition in {"removed-member", "deleted-group", "other-namespace", "unlisted-group", "wrong-settings-namespace"}:
        await directory_group(
            native, account.id, removed=condition == "removed-member", deleted=condition == "deleted-group",
            namespace="other-directory" if condition == "other-namespace" else None,
            external_id="not-allowed" if condition == "unlisted-group" else "allowed-external",
        )
    if condition == "other-user":
        other: Final = await created(native, "other")
        await directory_group(native, other.id)
    if condition in {"missing-settings", "wrong-settings-namespace"}:
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            if condition == "missing-settings":
                await writer.execute_raw("DELETE FROM retail_scim_settings WHERE singleton")
            else:
                await writer.execute_raw("UPDATE retail_scim_settings SET namespace='other-directory' WHERE singleton")
    monkeypatch.setenv("RETAIL_SCIM_ENABLED", "false")
    monkeypatch.delenv("RETAIL_SCIM_CONFIG", raising=False)
    with pytest.raises(HTTPException) as key_denied:
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    assert key_denied.value.status_code == 401 and token not in str(key_denied.value)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        with pytest.raises(HTTPException) as directory_denied:
            await scim_identity.require_scim_directory_user(writer, account.user_id, lock=True)
        with pytest.raises(HTTPException) as session_denied:
            await scim_identity.require_scim_session(writer, account.user_id, token)
        proof: Final = await writer.query_raw(
            """SELECT p.credential_revision=r.credential_revision AS current FROM retail_scim_credentials p
            JOIN retail_scim_users r USING(user_id) WHERE p.token_hash=$1""", token,
        )
    assert directory_denied.value.status_code == 401 and session_denied.value.status_code == 401
    assert proof == [{"current": True}]


@pytest.mark.asyncio
async def test_any_current_allowed_group_admits_without_granting_models_or_requiring_local_scim_config(
    native: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account: Final = await created(native)
    token: Final = await issued(native, account)
    await access_list(native, "missing-external", "allowed-external")
    await directory_group(native, account.id)
    monkeypatch.setenv("RETAIL_SCIM_ENABLED", "false")
    monkeypatch.delenv("RETAIL_SCIM_CONFIG", raising=False)
    resolved: Final = await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    assert resolved is not None and resolved.auth.models == ["fixture-model"] and resolved.user.models == []
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await scim_identity.require_scim_directory_user(writer, account.user_id, lock=True)
        await scim_identity.require_scim_session(writer, account.user_id, token)


@pytest.mark.asyncio
async def test_group_remove_readd_and_access_list_weakening_never_restore_old_key_proofs(native: Fixture) -> None:
    account: Final = await created(native)
    group: Final = await native.store.create_group(native.setup, GroupProfile(
        external_id="allowed-external", display_name="Allowed fixture", members=(account.id,),
    ))
    assert isinstance(group, GroupRecord)
    restricted: Final = ScimSetup(native.setup.config.model_copy(update={
        "generation": 2, "access_group_external_ids": (group.external_id,),
    }), native.setup.token)
    assert await native.store.configure(restricted) is None
    token: Final = await issued(native, account)
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token) is not None
    removed: Final = await native.store.update_group(
        restricted, group.id, patch({"op": "remove", "path": "members"}), replace=False, if_match=None,
    )
    assert isinstance(removed, GroupRecord) and removed.members == ()
    with pytest.raises(HTTPException):
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    restored: Final = await native.store.update_group(
        restricted, group.id, patch({"op": "add", "path": "members", "value": [{"value": str(account.id)}]}),
        replace=False, if_match=None,
    )
    assert isinstance(restored, GroupRecord) and restored.members == (account.id,)
    with pytest.raises(HTTPException):
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), token)
    fresh: Final = await issued(native, account, "fresh-group")
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), fresh) is not None
    unrestricted: Final = ScimSetup(restricted.config.model_copy(update={
        "generation": 3, "access_group_external_ids": (),
    }), restricted.token)
    assert await native.store.configure(unrestricted) is None
    with pytest.raises(HTTPException):
        await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), fresh)
    newest: Final = await issued(native, account, "unrestricted-group")
    assert await scim_identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=account.user_id), newest) is not None

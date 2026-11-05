import importlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request

import litellm
from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy import proxy_server
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.retail import coordination_guard
from litellm.proxy.retail import request_policy_runtime as runtime
from litellm.proxy.retail.billing import RetailBilling
from litellm.proxy.retail.coordination_guard import CoordinationMonitor, GuardConfig
from litellm.proxy.retail.request_policy_management import PolicyCreate, PolicyUpdate, PromptCreate
from litellm.proxy.retail.request_policy_types import Definition, RequestPolicyFailure
from tests.integration.database.test_retail_request_policy_store import (
    ACTOR,
    CUSTOMER_KEY,
    MODEL,
    PRIVATE,
    TEAM,
    Client,
    DirectWriter,
    Fixture,
    Writer,
    assigned,
    created,
    native,  # noqa: F401  # register the isolated PostgreSQL fixture
    rows,
)
from tests.unit.proxy.retail.test_api_jwt_native_auth import native_globals


@dataclass(frozen=True, slots=True)
class NativeClient:
    source: Client
    db: object = field(default_factory=object)

    @property
    def writer_db(self) -> DirectWriter:
        return self.source.writer_db

    def tx(self, *, timeout: timedelta) -> AbstractAsyncContextManager[Writer]:
        return self.source.tx(timeout=timeout)


async def healthy() -> bool:
    return True


async def recover() -> None:
    return None


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def native_runtime(native: Fixture, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monitor: Final = CoordinationMonitor(GuardConfig(required=True), probe=healthy, recover=recover)
    await monitor.check_once()
    await monitor.start()
    monkeypatch.setattr(coordination_guard._lifecycle, "monitor", monitor)
    monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(native.client))
    monkeypatch.setattr(proxy_server, "master_key", "sk-fixture-master")
    monkeypatch.setattr(proxy_server, "general_settings", {})
    monkeypatch.setattr(proxy_server, "user_custom_auth", None)
    monkeypatch.setattr(proxy_server, "llm_router", SimpleNamespace(get_model_list=lambda model, team_id: (
        {"model_info": {"id": "fixture-deployment"}, "litellm_params": {"model": "openai/fixture-backend"}},
    )))
    monkeypatch.setattr(litellm, "callbacks", (RetailBilling(),))
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REQUIRE_COORDINATION_REDIS", "true")
    monkeypatch.delenv("DATABASE_URL_READ_REPLICA", raising=False)
    try:
        yield
    finally:
        await monitor.close()


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})


def auth() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(api_key=CUSTOMER_KEY, token=CUSTOMER_KEY, user_id="ordinary-owner", user_role="customer")


@pytest.mark.asyncio
async def test_real_lookup_intersects_all_targets_and_keeps_private_prompt_out_of_prepared_body(native: Fixture) -> None:
    prompt: Final = await created(native, PromptCreate(name="Private fixture", content=PRIVATE), "prompts")
    for kind, cap in (("model", 500), ("user", 200), ("key", 300)):
        policy: Final = await created(native, PolicyCreate(name=kind, definition=Definition(
            max_output_tokens=cap, temperature=0.5, allowed_routes=("chat.completions",),
        ), prompt_id=UUID(str(prompt["id"]))))
        await assigned(native, kind, policy_id=UUID(str(policy["id"])))
    source: Final = {"model": MODEL, "messages": [{"role": "user", "content": "client request"}], "max_tokens": 1000}
    admitted: Final = await runtime.authorize_request_policies(request(), auth(), source, "/v1/chat/completions")
    prepared: Final = runtime.prepare_request_policy(admitted, source, "acompletion")
    assert prepared["max_tokens"] == 200 and prepared["temperature"] == 0.5 and PRIVATE not in str(prepared)
    final: Final = runtime.final_request_policy_deployment({**prepared, "model": "openai/fixture-backend",
        "model_info": {"id": "fixture-deployment"}}, "acompletion")
    assert final["messages"] == [{"role": "system", "content": "\n\n".join((PRIVATE, PRIVATE, PRIVATE))},
        {"role": "user", "content": "client request"}]
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
async def test_real_team_policy_intersects_four_scopes_and_an_authorized_move_uses_only_current_parent(native: Fixture) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    for kind, cap in (("model", 500), ("team", 32), ("user", 200), ("key", 300)):
        policy: Final = await created(native, PolicyCreate(name=kind, definition=Definition(max_output_tokens=cap)))
        await assigned(native, kind, policy_id=UUID(str(policy["id"])))
    source: Final = {"model": MODEL, "messages": [{"role": "user", "content": "client request"}]}
    cached: Final = auth().model_copy(update={"team_id": "stale-cached-parent", "team_models": ["obsolete"], "team_rpm_limit": 999})
    admitted: Final = await runtime.authorize_request_policies(request(), cached, source, "/v1/chat/completions")
    assert admitted.team_id == TEAM and admitted.team_max_budget == 21
    assert runtime.prepare_request_policy(admitted, source, "acompletion")["max_tokens"] == 32
    await rows(native, '''UPDATE "LiteLLM_VerificationToken" SET team_id='fixture-native-team-two' WHERE token=$1 RETURNING token''', CUSTOMER_KEY)
    moved: Final = await runtime.authorize_request_policies(request(), cached, source, "/v1/chat/completions")
    assert moved.team_id == "fixture-native-team-two" and moved.team_max_budget == 22
    assert runtime.prepare_request_policy(moved, source, "acompletion")["max_tokens"] == 200
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ("team-model", "member-model", "member-budget", "default-budget"))
async def test_real_joined_team_member_limits_reach_native_checks_before_any_wallet_work(
    native: Fixture, restriction: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await rows(native, 'UPDATE "LiteLLM_VerificationToken" SET team_id=$2 WHERE token=$1 RETURNING token', CUSTOMER_KEY, TEAM)
    await rows(native, '''INSERT INTO "LiteLLM_BudgetTable"(budget_id,max_budget,allowed_models,created_by,updated_by)
        VALUES('member-budget',$1,$2,'fixture-admin','fixture-admin') RETURNING budget_id''',
        1 if restriction == "member-budget" else None, ["revoked-model"] if restriction == "member-model" else [])
    await rows(native, '''INSERT INTO "LiteLLM_TeamMembership"(user_id,team_id,budget_id,spend)
        VALUES('ordinary-owner',$1,'member-budget',3) RETURNING team_id''', TEAM)
    if restriction == "team-model":
        await rows(native, '''UPDATE "LiteLLM_TeamTable" SET models=ARRAY['revoked-model'] WHERE team_id=$1 RETURNING team_id''', TEAM)
    if restriction == "default-budget":
        await rows(native, '''INSERT INTO "LiteLLM_BudgetTable"(budget_id,max_budget,created_by,updated_by)
            VALUES('default-budget',1,'fixture-admin','fixture-admin') RETURNING budget_id''')
        await rows(native, '''UPDATE "LiteLLM_TeamTable" SET metadata='{"team_member_budget_id":"default-budget"}' WHERE team_id=$1 RETURNING team_id''', TEAM)
    admitted: Final = await runtime.authorize_request_policies(request(), auth(), {"model": MODEL}, "/v1/chat/completions")
    assert runtime.request_policy_team_membership() is not None
    native_globals(monkeypatch)
    module: Final = importlib.import_module("litellm.proxy.auth.user_api_key_auth")
    with pytest.raises((litellm.BudgetExceededError, HTTPException, ProxyException)):
        await module.run_centralized_common_checks(admitted, request(), {"model": MODEL}, "/v1/chat/completions", force_virtual_key_checks=True)
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("role", "owner", "blocked", "password-reset"))
async def test_current_database_identity_rejects_cached_authority_before_wallet(native: Fixture, change: str) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        if change == "role":
            await writer.execute_raw('''UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin' WHERE user_id='ordinary-owner' ''')
        elif change == "owner":
            await writer.execute_raw('''UPDATE "LiteLLM_VerificationToken" SET user_id='admin-owner' WHERE token=$1''', CUSTOMER_KEY)
        elif change == "blocked":
            await writer.execute_raw('UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token=$1', CUSTOMER_KEY)
        else:
            await writer.execute_raw('''UPDATE "LiteLLM_UserTable" SET password_reset_required=true WHERE user_id='ordinary-owner' ''')
    with pytest.raises(HTTPException) as denied:
        await runtime.authorize_request_policies(request(), auth(), {"model": MODEL}, "/v1/chat/completions")
    assert denied.value.status_code == 403 and CUSTOMER_KEY not in str(denied.value)
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]


@pytest.mark.asyncio
async def test_new_auth_reads_policy_version_without_cache_and_disabled_feature_does_not_read_it(native: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    policy: Final = await created(native, PolicyCreate(name="Live cap", definition=Definition(max_output_tokens=100)))
    await assigned(native, policy_id=UUID(str(policy["id"])))
    source: Final = {"model": MODEL, "messages": [{"role": "user", "content": "client request"}]}
    first: Final = await runtime.authorize_request_policies(request(), auth(), source, "/v1/chat/completions")
    assert runtime.prepare_request_policy(first, source, "acompletion")["max_tokens"] == 100
    changed: Final = await native.store.write(ACTOR, "policies", "updated", uuid4().hex,
        PolicyUpdate(name="Live cap", definition=Definition(max_output_tokens=32), expected_version=1), UUID(str(policy["id"])), 1)
    assert not isinstance(changed, RequestPolicyFailure)
    second: Final = await runtime.authorize_request_policies(request(), auth(), source, "/v1/chat/completions")
    assert runtime.prepare_request_policy(second, source, "acompletion")["max_tokens"] == 32
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "false")
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("DROP TABLE retail_request_policy_bindings CASCADE")
    assert await runtime.authorize_request_policies(request(), first, source, "/v1/chat/completions") is first


@pytest.mark.asyncio
async def test_stateless_master_model_rule_is_enforced_before_administrator_billing_exemption(native: Fixture) -> None:
    policy: Final = await created(native, PolicyCreate(name="Master bounded", definition=Definition(max_output_tokens=16)))
    await assigned(native, "model", policy_id=UUID(str(policy["id"])))
    master: Final = UserAPIKeyAuth(api_key=LITELLM_PROXY_MASTER_KEY_ALIAS, user_id="stateless-native-admin",
        user_role="proxy_admin").model_copy(update={"via_virtual_key": True})
    source: Final = {"model": MODEL, "messages": [{"role": "user", "content": "client request"}], "max_tokens": 1000}
    admitted: Final = await runtime.authorize_request_policies(request(), master, source, "/v1/chat/completions")
    prepared: Final = await RetailBilling()._prepare_request(admitted, source, "acompletion")
    assert prepared["max_tokens"] == 16
    assert await rows(native, "SELECT * FROM retail_wallets") == [{"user_id": "ordinary-owner", "balance_nanos": 123456789}]

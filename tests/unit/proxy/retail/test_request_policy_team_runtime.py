import importlib
from typing import Final
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

import litellm
from litellm.proxy._types import (
    UI_TEAM_ID,
    LiteLLM_BudgetTable,
    LiteLLM_ModelTable,
    LiteLLM_TeamMembership,
    LiteLLM_TeamTable,
    ProxyException,
    UserAPIKeyAuth,
)
from litellm.proxy.auth import auth_checks
from litellm.proxy.retail import request_policy_runtime as runtime
from litellm.proxy.retail.request_policy_types import ApplicablePolicy, Definition, ResolvedRequestPolicies
from tests.unit.proxy.retail.test_api_jwt_native_auth import native_globals
from tests.unit.proxy.retail.test_request_policy_runtime import _AUTH, _HASH, _USER, body, request

_NATIVE: Final = importlib.import_module("litellm.proxy.auth.user_api_key_auth")


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    native_globals(monkeypatch)
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")
    monkeypatch.setattr(runtime, "_require_environment", lambda: None)


async def admit(cached: UserAPIKeyAuth, current: UserAPIKeyAuth, team: LiteLLM_TeamTable | None,
    member: LiteLLM_TeamMembership | None = None, default_budget: LiteLLM_BudgetTable | None = None,
) -> UserAPIKeyAuth:
    async def lookup(auth: UserAPIKeyAuth, model: str) -> ResolvedRequestPolicies:
        assert auth is cached and model == "fixture-model"
        return ResolvedRequestPolicies(current, _USER, _HASH, (
            ApplicablePolicy("team", UUID(int=2), 1, Definition(max_output_tokens=32)),
        ) if team else (), team, member, default_budget)
    return await runtime.authorize_request_policies(request(), cached, body(), "/v1/chat/completions", lookup=lookup)


@pytest.mark.asyncio
async def test_current_key_team_replaces_all_cached_parent_grants_and_identity() -> None:
    cached: Final = _AUTH.model_copy(update={"team_id": "team-a", "team_alias": "old-a", "team_models": ["old"],
        "team_blocked": True, "team_rpm_limit": 999, "team_member_rpm_limit": 999,
        "team_member_spend": 999, "team_model_aliases": {"old": "different"}, "org_id": "old-org",
        "organization_rpm_limit": 999, "organization_metadata": {"former": "a"}})
    current: Final = _AUTH.model_copy(update={"team_id": "team-b"})
    team: Final = LiteLLM_TeamTable(team_id="team-b", team_alias="current-b", models=["fixture-model"],
        rpm_limit=7, tpm_limit=11, max_budget=15, spend=2,
        members_with_roles=[{"user_id": _USER.user_id, "role": "admin"}],
        litellm_model_table=LiteLLM_ModelTable(model_aliases={"short": "fixture-model"}, created_by="admin", updated_by="admin"))
    member: Final = LiteLLM_TeamMembership(user_id=_USER.user_id, team_id="team-b", spend=3,
        litellm_budget_table=LiteLLM_BudgetTable(rpm_limit=5, tpm_limit=9))
    admitted: Final = await admit(cached, current, team, member)
    assert admitted.team_id == "team-b" and admitted.team_alias == "current-b" and tuple(admitted.team_models) == ("fixture-model",)
    assert admitted.team_rpm_limit == 7 and admitted.team_tpm_limit == 11 and admitted.team_member_rpm_limit == 5
    assert admitted.team_member_spend == 3 and admitted.team_model_aliases == {"short": "fixture-model"}
    assert not admitted.team_blocked and admitted.user_role == "customer" and admitted.team_member.role == "admin"
    assert admitted.org_id is None and admitted.organization_rpm_limit is None and admitted.organization_metadata is None
    assert runtime.request_policy_team().team_id == "team-b" and runtime.request_policy_team_membership() is member
    assert "old-a" not in repr(runtime._context.get())


@pytest.mark.asyncio
async def test_current_key_with_no_team_clears_all_old_parent_fields() -> None:
    cached: Final = _AUTH.model_copy(update={"team_id": "team-a", "team_blocked": True, "team_models": ["old"],
        "team_metadata": {"secret": "old-team"}, "team_member_spend": 9, "team_rpm_limit": 1})
    admitted: Final = await admit(cached, _AUTH, None)
    assert admitted.team_id is None and not admitted.team_blocked and admitted.team_models == []
    assert admitted.team_metadata is None and admitted.team_member_spend is None and admitted.team_rpm_limit is None
    assert runtime.request_policy_team() is None and runtime.request_policy_team_membership() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("missing", "wrong-team", "blocked", "wrong-member", "orphan-member"))
async def test_missing_or_inconsistent_current_team_authority_is_rejected(state: str) -> None:
    current: Final = _AUTH.model_copy(update={"team_id": None if state == "orphan-member" else "team-b"})
    team: Final = None if state in {"missing", "orphan-member"} else LiteLLM_TeamTable(
        team_id="other" if state == "wrong-team" else "team-b", blocked=state == "blocked")
    member: Final = LiteLLM_TeamMembership(user_id="different", team_id="team-b") if state in {"wrong-member", "orphan-member"} else None
    with pytest.raises(HTTPException) as failure:
        await admit(_AUTH, current, team, member)
    assert failure.value.status_code == 403 and _HASH not in str(failure.value)


@pytest.mark.asyncio
async def test_dashboard_team_sentinel_retains_native_ui_scope_without_a_team_row() -> None:
    current: Final = _AUTH.model_copy(update={"team_id": UI_TEAM_ID})
    admitted: Final = await admit(current, current, None)
    assert admitted.team_id == UI_TEAM_ID and runtime.request_policy_team() is None


@pytest.mark.asyncio
async def test_final_or_recovered_auth_cannot_replace_the_current_parent_team() -> None:
    current: Final = _AUTH.model_copy(update={"team_id": "team-b"})
    admitted: Final = await admit(_AUTH, current, LiteLLM_TeamTable(team_id="team-b"))
    for replacement in (None, "team-a"):
        with pytest.raises(HTTPException) as failure:
            runtime.require_request_policy_result(admitted.model_copy(update={"team_id": replacement}), inference=True)
        assert failure.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ("team-model", "team-budget", "member-model", "member-budget", "default-budget"))
async def test_real_native_common_checks_use_fresh_team_and_member_limits_without_cache_rehydration(
    restriction: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current: Final = _AUTH.model_copy(update={"team_id": "team-b", "models": ["all-team-models"]})
    team: Final = LiteLLM_TeamTable(team_id="team-b", models=["revoked-model"] if restriction == "team-model" else ["fixture-model"],
        max_budget=1 if restriction == "team-budget" else None, spend=2,
        metadata={"team_member_budget_id": "current-default"} if restriction == "default-budget" else {})
    member: Final = LiteLLM_TeamMembership(user_id=_USER.user_id, team_id="team-b", spend=3,
        litellm_budget_table=LiteLLM_BudgetTable(max_budget=1 if restriction == "member-budget" else None,
            allowed_models=["revoked-model"] if restriction == "member-model" else []))
    default: Final = LiteLLM_BudgetTable(max_budget=1) if restriction == "default-budget" else None
    admitted: Final = await admit(_AUTH, current, team, member, default)
    stale_team: Final = AsyncMock(return_value=LiteLLM_TeamTable(team_id="team-a", models=[]))
    stale_member: Final = AsyncMock(return_value=None)
    monkeypatch.setattr(_NATIVE, "get_team_object", stale_team)
    monkeypatch.setattr(auth_checks, "get_team_membership", stale_member)
    monkeypatch.setattr(auth_checks, "get_team_member_default_budget", stale_member)
    with pytest.raises((ProxyException, litellm.BudgetExceededError)):
        await _NATIVE.run_centralized_common_checks(admitted, request(), dict(body()), "/v1/chat/completions", force_virtual_key_checks=True)
    stale_team.assert_not_awaited()
    stale_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_revalidation_reuses_joined_team_and_member_without_second_cache_read(monkeypatch: pytest.MonkeyPatch) -> None:
    team: Final = LiteLLM_TeamTable(team_id="team-b", models=["fixture-model"], rpm_limit=7)
    current: Final = _AUTH.model_copy(update={"team_id": "team-b", "models": ["all-team-models"]})
    admitted: Final = await admit(_AUTH, current, team)
    stale: Final = AsyncMock(side_effect=AssertionError("must not read stale Team cache"))
    monkeypatch.setattr(_NATIVE, "get_team_object", stale)
    checked: Final = await _NATIVE.revalidate_request_policy_key(request(), admitted, body(), "/v1/chat/completions")
    assert checked.team_id == "team-b" and checked.team_rpm_limit == 7
    stale.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", (False, True))
async def test_new_native_auth_clears_previous_same_task_team_authority_before_early_alias_resolution(
    internal: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await admit(_AUTH, _AUTH.model_copy(update={"team_id": "team-b"}), LiteLLM_TeamTable(team_id="team-b"))
    assert runtime.request_policy_team() is not None

    async def stop_before_authority(**kwargs: object) -> None:  # kwargs-ok: intercept two native entry signatures before any identity I/O
        assert not runtime.request_policy_attested() and runtime.request_policy_team() is None
        raise HTTPException(401, "fixture stops before native identity lookup")

    boundary: Final = AsyncMock(side_effect=stop_before_authority)
    selected: Final = request()
    selected._body = b'{"model":"fixture-model"}'
    if internal:
        monkeypatch.setattr(_NATIVE.IdentityStore, "resolve", boundary)
    else:
        monkeypatch.setattr(_NATIVE, "user_api_key_auth_builder", boundary)
    with pytest.raises((HTTPException, ProxyException)):
        if internal:
            await _NATIVE.authorize_internal_virtual_key(_HASH, selected, dict(body()))
        else:
            await _NATIVE.user_api_key_auth(selected, None, None, None, None, None, None)
    boundary.assert_awaited_once()

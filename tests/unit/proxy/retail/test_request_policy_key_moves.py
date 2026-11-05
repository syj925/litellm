from dataclasses import dataclass
from typing import Final, Literal

import pytest
from fastapi import HTTPException

from litellm.proxy._types import (
    LiteLLM_VerificationToken,
    LitellmUserRoles,
    RegenerateKeyRequest,
    UpdateKeyRequest,
    UserAPIKeyAuth,
)
from litellm.proxy.management_endpoints.key_management_endpoints import prepare_key_update_data
from litellm.proxy.retail.request_policy_key_moves import guard_key_team_change
from litellm.proxy.retail.request_policy_management import PolicyActor
from litellm.proxy.retail.request_policy_types import RequestPolicyFailure


@dataclass(frozen=True, slots=True)
class Verify:
    result: RequestPolicyFailure | None = None

    async def __call__(self, actor: PolicyActor) -> RequestPolicyFailure | None:
        assert actor.user_id == "operator" and actor.token_hash == "a" * 64
        assert actor.trusted_master is False and actor.request_route == "/key/regenerate"
        return self.result


async def unexpected_verify(actor: PolicyActor) -> RequestPolicyFailure | None:
    raise AssertionError("This operation must not read policy authority")


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "true")


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", (None, "team-b"))
async def test_team_member_cannot_detach_or_reparent_a_key(requested: str | None) -> None:
    auth: Final = UserAPIKeyAuth(user_id="member", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as denied:
        await guard_key_team_change(auth, "team-a", requested, provided=True, verify=unexpected_verify)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("provided,previous,requested", (
    (False, "team-a", None), (True, "team-a", "team-a"), (True, None, None),
))
async def test_same_team_and_ordinary_rotation_need_no_policy_lookup(
    provided: bool, previous: str | None, requested: str | None,
) -> None:
    assert await guard_key_team_change(None, previous, requested, provided=provided, verify=unexpected_verify) is None


@pytest.mark.asyncio
async def test_disabled_policy_mode_preserves_legacy_key_team_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUEST_POLICIES_ENABLED", "false")
    assert await guard_key_team_change(None, "team-a", None, provided=True, verify=unexpected_verify) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", ((None, None), ("forbidden", 403), ("unavailable", 503), ("capacity", 503)))
async def test_admin_team_changes_require_current_writer_authority(
    failure: Literal["forbidden", "unavailable", "capacity"] | None, status: int | None,
) -> None:
    auth: Final = UserAPIKeyAuth(user_id="operator", user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key="a" * 64, token="a" * 64, request_route="/key/regenerate")
    verifier: Final = Verify(RequestPolicyFailure(failure) if failure is not None else None)
    if status is None:
        await guard_key_team_change(auth, "team-a", None, provided=True, verify=verifier)
        return
    with pytest.raises(HTTPException) as denied:
        await guard_key_team_change(auth, "team-a", None, provided=True, verify=verifier)
    assert denied.value.status_code == status


@pytest.mark.asyncio
async def test_custom_auth_cannot_claim_proxy_admin_for_parent_changes() -> None:
    auth: Final = UserAPIKeyAuth(user_id="operator", user_role=LitellmUserRoles.PROXY_ADMIN).model_copy(
        update={"authenticated_by_custom_auth": True})
    with pytest.raises(HTTPException) as denied:
        await guard_key_team_change(auth, "team-a", "team-b", provided=True, verify=unexpected_verify)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("regenerate", (False, True))
async def test_native_update_and_regenerate_cannot_write_a_member_detachment(regenerate: bool) -> None:
    payload: Final = {"key": "a" * 64, "team_id": None}
    change: Final = RegenerateKeyRequest(**payload) if regenerate else UpdateKeyRequest(**payload)
    current: Final = LiteLLM_VerificationToken(token="a" * 64, user_id="member", team_id="team-a")
    auth: Final = UserAPIKeyAuth(user_id="member", user_role=LitellmUserRoles.INTERNAL_USER)
    with pytest.raises(HTTPException) as denied:
        await prepare_key_update_data(change, current, user_api_key_dict=auth)
    assert denied.value.status_code == 403 and current.team_id == "team-a"

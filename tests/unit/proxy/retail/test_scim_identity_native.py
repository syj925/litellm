import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

import litellm
from litellm.proxy import proxy_server
from litellm.proxy._types import LiteLLM_UserTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.retail import oidc_sessions
from litellm.proxy.retail import scim_identity as identity
from tests.unit.proxy.retail.test_api_jwt_native_auth import _AUTH, native_globals
from tests.unit.proxy.retail.test_scim_identity import _HASH, _RAW, _USER, Reader, request, row


@pytest.fixture(autouse=True)
def native_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    native_globals(monkeypatch)
    monkeypatch.setattr(identity, "_require_environment", lambda: None)


@pytest.mark.asyncio
async def test_native_builder_refreshes_proof_before_cached_admin_shortcut_and_injects_owner_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    owner: Final = LiteLLM_UserTable(user_id=_USER, user_role="customer", rpm_limit=17, tpm_limit=29)
    current: Final = UserAPIKeyAuth(api_key=_HASH, token=_HASH, user_id=_USER, user_role="customer")
    resolved: Final = identity.ResolvedScimIdentity(current, owner, _HASH)
    native_read: Final = AsyncMock(return_value=resolved)
    monkeypatch.setattr(identity, "_native_identity", native_read)
    cache: Final = UserApiKeyCache()
    await cache.async_set_cache(key=_HASH, value=current.model_copy(update={"user_role": "proxy_admin"}), model_type=UserAPIKeyAuth)
    await cache.async_set_cache(key=_USER, value=LiteLLM_UserTable(user_id=_USER, user_role="proxy_admin"), model_type=LiteLLM_UserTable)
    monkeypatch.setattr(proxy_server, "prisma_client", SimpleNamespace(db=object()))
    monkeypatch.setattr(proxy_server, "user_api_key_cache", cache)
    selected: Final = request()
    admitted: Final = await _AUTH.user_api_key_auth_builder(selected, "Bearer " + _RAW, None, None, None, None, {})
    await asyncio.sleep(0)
    assert admitted.user_role == "customer" and admitted.user_rpm_limit == 17 and admitted.user_tpm_limit == 29
    assert admitted.api_key == _HASH and admitted.token == _HASH
    native_read.assert_awaited_once_with(_HASH, _USER)
    identity.require_scim_identity_result(selected, admitted)


@pytest.mark.asyncio
async def test_native_common_checks_enforce_current_managed_owner_budget_without_cached_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: Final = request()
    source_row: Final = row()
    resolved: Final = await identity.resolve_scim_identity(
        selected, UserAPIKeyAuth(user_id=_USER), _RAW,
        reader=Reader(({**source_row, "user_data": {"user_id": _USER, "user_role": "customer", "max_budget": 1, "spend": 2}},)),
    )
    assert resolved is not None
    cached: Final = AsyncMock(return_value=LiteLLM_UserTable(user_id=_USER, user_role="proxy_admin"))
    monkeypatch.setattr(_AUTH, "get_user_object", cached)
    with pytest.raises(litellm.BudgetExceededError) as failure:
        await _AUTH.run_centralized_common_checks(
            user_api_key_auth_obj=resolved.auth, request=selected,
            request_data={"model": "allowed-model"}, route="/v1/chat/completions", force_virtual_key_checks=True,
        )
    assert failure.value.max_budget == 1 and failure.value.current_cost == 2
    cached.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", (False, True))
async def test_wrapper_rejects_managed_auth_that_loses_its_proof_or_is_promoted_after_recovery(
    recovered: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: Final = request()
    selected._body = json.dumps({"model": "allowed-model"}).encode()
    resolved: Final = await identity.resolve_scim_identity(selected, UserAPIKeyAuth(user_id=_USER), _RAW, reader=Reader((row(),)))
    assert resolved is not None
    if not recovered:
        selected.scope.clear()
        selected.scope.update({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
    monkeypatch.setattr(_AUTH, "user_api_key_auth_builder", AsyncMock(return_value=resolved.auth))
    authorization: Final = AsyncMock(return_value=resolved.auth.model_copy(update={"user_role": "proxy_admin"}) if recovered else None)
    monkeypatch.setattr(_AUTH, "_authorize_authenticated_request", authorization)
    with pytest.raises(HTTPException) as failure:
        await _AUTH.user_api_key_auth(selected, None, None, None, None, None, None)
    assert failure.value.status_code == 401 and _USER not in str(failure.value)
    if not recovered:
        authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrapper_forces_standard_checks_for_managed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: Final = request()
    selected._body = json.dumps({"model": "allowed-model"}).encode()
    source_row: Final = row()
    resolved: Final = await identity.resolve_scim_identity(
        selected, UserAPIKeyAuth(user_id=_USER), _RAW,
        reader=Reader(({**source_row, "user_data": {"user_id": _USER, "user_role": "customer", "max_budget": 1, "spend": 2}},)),
    )
    assert resolved is not None
    monkeypatch.setattr(_AUTH, "user_api_key_auth_builder", AsyncMock(return_value=resolved.auth))
    monkeypatch.setattr(proxy_server, "general_settings", {
        "disable_budget_reservation": True, "pass_through_endpoints": [{"path": "/v1/chat/completions", "auth": False}],
    })
    with pytest.raises(ProxyException) as failure:
        await _AUTH.user_api_key_auth(selected, None, None, None, None, None, None)
    assert failure.value.code == "401" and _USER not in str(failure.value.to_dict())


class OidcReader:
    def __init__(self, *, proof: bool) -> None:
        self.proof = proof
        self.calls = 0

    async def query_raw(self, query: str, *args: object) -> object:
        self.calls += 1
        if "retail_oidc_sessions p" in query:
            return ({
                "user_id": _USER, "user_role": "customer", "password_reset_required": False,
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=100), "session_id": str(UUID(int=1)),
                "metadata": {"login_method": "retail_oidc"}, "allowed_routes": list(oidc_sessions.OIDC_SESSION_ROUTES),
            },)
        native_key: Final = oidc_sessions.hash_token(oidc_sessions.OIDC_SESSION_PREFIX + "fixture-native-session")
        return ({"key_data": {"token": native_key, "user_id": _USER},
            "user_data": {"user_id": _USER, "user_role": "customer"}},) if self.proof else ()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", (False, True))
async def test_oidc_fresh_session_requires_managed_credential_proof_even_with_valid_native_session(proof: bool) -> None:
    source: Final = OidcReader(proof=proof)
    auth: Final = UserAPIKeyAuth(user_id=_USER, user_role="proxy_admin", metadata={"login_method": "retail_oidc"})
    if not proof:
        with pytest.raises(HTTPException) as failure:
            await oidc_sessions.fresh_oidc_session(auth, oidc_sessions.OIDC_SESSION_PREFIX + "fixture-native-session", "/user/info", "GET", reader=source)
        assert failure.value.status_code == 401
    else:
        admitted: Final = await oidc_sessions.fresh_oidc_session(auth, oidc_sessions.OIDC_SESSION_PREFIX + "fixture-native-session", "/user/info", "GET", reader=source)
        assert admitted.user_role == "customer"
    assert source.calls == 2


@pytest.mark.asyncio
async def test_configured_native_admin_identity_cannot_use_managed_uid_for_password_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.proxy.auth import login_utils

    monkeypatch.setenv("UI_USERNAME", "fixture-native-admin")
    monkeypatch.setenv("UI_PASSWORD", "fixture-native-password")
    monkeypatch.setenv("PROXY_ADMIN_ID", _USER)
    with pytest.raises(HTTPException) as failure:
        await login_utils._sign_in("fixture-native-admin", "fixture-native-password", "sk-fixture-master", None, AsyncMock(), {})
    assert failure.value.status_code == 403


class OidcMintWriter:
    def __init__(self, settings: oidc_sessions.SessionSettings) -> None:
        self.settings = settings
        self.writes = 0

    async def query_raw(self, query: str, *args: object) -> object:
        if "FROM retail_scim_users" in query:
            return ()
        if "FROM retail_oidc_settings" in query:
            return ({"namespace": self.settings.namespace, "generation": self.settings.generation,
                "digest": self.settings.digest, "issuer": self.settings.issuer, "session_seconds": self.settings.session_seconds},)
        if "FROM retail_oidc_bindings" in query:
            return ({"id": str(UUID(int=1)), "revision": str(UUID(int=2)), "user_id": _USER, "enabled": True},)
        if 'FROM "LiteLLM_UserTable"' in query:
            return ({"user_id": _USER, "user_role": "customer", "password_reset_required": False},)
        if "AS full" in query:
            return ({"full": False},)
        self.writes += 1
        return ()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("configure", "mint"))
async def test_oidc_checks_managed_directory_before_binding_or_session_side_effects(operation: str) -> None:
    settings: Final = oidc_sessions.SessionSettings(
        namespace="fixture-oidc", generation=1, digest="a" * 64, issuer="https://issuer.fixture.invalid",
        session_seconds=300, bindings=(oidc_sessions.BindingSpec(subject="fixture-subject", user_id=_USER),),
    )
    writer: Final = OidcMintWriter(settings)
    with pytest.raises(HTTPException) as failure:
        if operation == "configure":
            await oidc_sessions._configure_binding(writer, settings, settings.bindings[0])
        else:
            await oidc_sessions._mint_user(writer, settings, "fixture-subject")
    assert failure.value.status_code == 401 and writer.writes == 0

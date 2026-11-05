from collections.abc import Mapping
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from litellm.proxy._types import LiteLLM_UserTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.retail import scim_identity as identity
from litellm.proxy.utils import hash_token

_USER: Final = "retail-scim-user:00000000-0000-0000-0000-000000000001"
_RAW: Final = "sk-fixture-private-managed-credential"
_HASH: Final = hash_token(_RAW)
_ENVIRONMENT: Final = identity._require_environment


@pytest.fixture(autouse=True)
def healthy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "_require_environment", lambda: None)


def request() -> Request:
    return Request({"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": []})


def row() -> Mapping[str, object]:
    return {
        "key_data": {"token": _HASH, "user_id": _USER, "models": ["allowed-model"], "rpm_limit": 11},
        "user_data": {"user_id": _USER, "user_role": "customer", "rpm_limit": 17, "tpm_limit": 29, "max_budget": 7},
    }


class Reader:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: tuple[tuple[str, tuple[object, ...]], ...] = ()

    async def query_raw(self, query: str, *args: object) -> object:
        self.calls = (*self.calls, (query, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", (None, "ordinary-user", "default_user_id", "retail-scim-userish"))
async def test_nonmanaged_identity_adds_no_authority_read_or_environment_dependency(user_id: str | None) -> None:
    source: Final = Reader(RuntimeError("authority must not run"))
    auth: Final = UserAPIKeyAuth(user_id=user_id)
    selected: Final = request()
    assert await identity.resolve_scim_identity(selected, auth, _RAW, reader=source) is None
    assert source.calls == () and not identity.scim_identity_request(selected)
    assert identity.scim_identity_user(selected) is None
    identity.require_scim_identity_result(selected, auth)


@pytest.mark.asyncio
@pytest.mark.parametrize("presented", (_RAW, _HASH))
async def test_exact_native_projection_retains_key_limits_and_fresh_customer_without_bearer_scope(presented: str) -> None:
    source: Final = Reader((row(),))
    selected: Final = request()
    resolved: Final = await identity.resolve_scim_identity(
        selected, UserAPIKeyAuth(user_id=_USER, user_role="proxy_admin"), presented, reader=source,
    )
    assert resolved is not None
    assert source.calls[0][1] == (_HASH, _USER)
    assert resolved.auth.models == ["allowed-model"] and resolved.auth.rpm_limit == 11
    assert resolved.user.rpm_limit == 17 and resolved.user.max_budget == 7
    assert resolved.auth.user_role == "customer" and resolved.auth.jwt_claims is None
    assert resolved.auth.api_key == _HASH and resolved.auth.token == _HASH and resolved.auth.via_virtual_key
    assert identity.scim_identity_user(selected) is resolved.user
    assert identity.effective_scim_key(selected, _RAW) == _HASH
    assert _RAW not in repr(selected.scope) and _USER not in repr(selected.scope)
    identity.require_scim_identity_result(selected, resolved.auth)


@pytest.mark.asyncio
@pytest.mark.parametrize("result,status", (((), 401), (RuntimeError(_RAW), 503), (({"malformed": _RAW},), 503)))
async def test_missing_or_unavailable_native_authority_cannot_reuse_cached_grants(result: object, status: int) -> None:
    selected: Final = request()
    with pytest.raises(HTTPException) as failure:
        await identity.resolve_scim_identity(selected, UserAPIKeyAuth(user_id=_USER), _RAW, reader=Reader(result))
    assert failure.value.status_code == status and _RAW not in str(failure.value)
    assert identity.scim_identity_request(selected) and identity.scim_identity_user(selected) is None
    with pytest.raises(HTTPException):
        identity.require_scim_identity_result(selected, UserAPIKeyAuth(user_id=_USER))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", (
    {"token": "b" * 64}, {"user_id": "other-owner"},
))
async def test_resolver_never_accepts_a_grace_substitution_or_different_key_owner(change: Mapping[str, object]) -> None:
    projected: Final = row()
    source: Final = Reader(({**projected, "key_data": {"token": _HASH, "user_id": _USER, **change}},))
    with pytest.raises(HTTPException) as failure:
        await identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=_USER), _RAW, reader=source)
    assert failure.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("change", (
    {"user_id": "other"}, {"user_role": "proxy_admin"}, {"api_key": "b" * 64}, {"token": "b" * 64},
    {"jwt_claims": {"sub": "private-directory-subject"}}, {"is_session_token": True},
    {"authenticated_by_custom_auth": True},
))
async def test_final_and_recovered_auth_cannot_replace_verified_native_authority(change: Mapping[str, object]) -> None:
    selected: Final = request()
    resolved: Final = await identity.resolve_scim_identity(selected, UserAPIKeyAuth(user_id=_USER), _RAW, reader=Reader((row(),)))
    assert resolved is not None
    with pytest.raises(HTTPException) as failure:
        identity.require_scim_identity_result(selected, resolved.auth.model_copy(update=change))
    assert failure.value.status_code == 401


@pytest.mark.asyncio
async def test_objectless_cli_identity_and_missing_final_proof_fail_before_lookup() -> None:
    source: Final = Reader((row(),))
    with pytest.raises(HTTPException):
        await identity.resolve_scim_identity(request(), UserAPIKeyAuth(user_id=_USER, is_session_token=True), _RAW, reader=source)
    assert source.calls == ()
    with pytest.raises(HTTPException):
        identity.require_scim_identity_result(request(), UserAPIKeyAuth(user_id=_USER))


@pytest.mark.parametrize("managed", (False, True))
def test_managed_password_login_is_unconditionally_refused(managed: bool) -> None:
    if managed:
        with pytest.raises(HTTPException) as failure:
            identity.reject_scim_password_login(_USER)
        assert failure.value.status_code == 403 and _USER not in str(failure.value)
    else:
        identity.reject_scim_password_login("ordinary-user")


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", (False, True))
@pytest.mark.parametrize("active", (False, True))
async def test_oidc_directory_admission_is_exact_and_adds_no_read_for_nonmanaged(managed: bool, active: bool) -> None:
    user_id: Final = _USER if managed else "ordinary-user"
    source: Final = Reader(({"user_id": user_id},) if active else ())
    if managed and not active:
        with pytest.raises(HTTPException) as failure:
            await identity.require_scim_directory_user(source, user_id, lock=True)
        assert failure.value.status_code == 401
    else:
        await identity.require_scim_directory_user(source, user_id, lock=True)
    assert len(source.calls) == int(managed)
    if managed:
        assert source.calls[0][1] == (_USER,) and source.calls[0][0].endswith("FOR SHARE OF r")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 402, 403, 429, 503))
async def test_managed_error_projection_keeps_status_without_exposing_credentials_or_identity(status: int) -> None:
    selected: Final = request()
    await identity.resolve_scim_identity(selected, UserAPIKeyAuth(user_id=_USER), _RAW, reader=Reader((row(),)))
    original: Final = ProxyException(message=_RAW + _USER, type="auth_error", param=_HASH, code=status)
    projected: Final = identity.private_scim_identity_exception(original, selected)
    assert isinstance(projected, ProxyException) and projected.code == str(status)
    assert all(value not in str(projected.to_dict()) for value in (_RAW, _HASH, _USER))
    assert identity.private_scim_identity_exception(original, request()) is original


@pytest.mark.parametrize("health", (
    SimpleNamespace(required=False, accepting_credentials=True),
    SimpleNamespace(required=True, accepting_credentials=False),
))
def test_managed_mode_needs_required_and_currently_healthy_coordination(
    health: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    monkeypatch.setattr(identity, "api_jwt_compatible", lambda: True)
    monkeypatch.setattr(identity, "coordination_guard_status", lambda: health)
    with pytest.raises(HTTPException) as failure:
        _ENVIRONMENT()
    assert failure.value.status_code == 503


def test_managed_mode_needs_actual_billing_and_native_safe_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "coordination_guard_status", lambda: SimpleNamespace(required=True, accepting_credentials=True))
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "false")
    monkeypatch.setattr(identity, "api_jwt_compatible", lambda: True)
    with pytest.raises(HTTPException):
        _ENVIRONMENT()
    monkeypatch.setenv("RETAIL_BILLING_ENABLED", "true")
    monkeypatch.setattr(identity, "api_jwt_compatible", lambda: False)
    with pytest.raises(HTTPException):
        _ENVIRONMENT()
    monkeypatch.setattr(identity, "api_jwt_compatible", lambda: True)
    _ENVIRONMENT()


@pytest.mark.asyncio
async def test_actual_native_password_signin_refuses_managed_row_before_password_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.proxy.auth import login_utils
    from litellm.proxy.utils import hash_password

    monkeypatch.setenv("UI_USERNAME", "fixture-native-admin")
    monkeypatch.setenv("UI_PASSWORD", "fixture-native-password")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/not-used")
    mint: Final = AsyncMock(return_value={"token": "sk-fixture-new-session"})
    monkeypatch.setattr(login_utils, "generate_key_helper_fn", mint)
    database: Final = SimpleNamespace(db=SimpleNamespace(litellm_usertable=SimpleNamespace(
        find_first=AsyncMock(return_value=LiteLLM_UserTable(user_id=_USER, user_email="fixture@example.invalid", user_role="customer", password=hash_password("fixture-password"))),
    )))
    with pytest.raises(HTTPException) as failure:
        await login_utils._sign_in("fixture@example.invalid", "fixture-password", "sk-fixture-master", database, AsyncMock(), {})
    assert failure.value.status_code == 403 and _USER not in str(failure.value)
    mint.assert_not_awaited()

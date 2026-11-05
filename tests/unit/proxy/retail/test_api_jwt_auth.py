from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from litellm.proxy._types import LiteLLM_UserTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.retail.api_jwt_auth import (
    API_JWT_SCOPE_KEY,
    ApiJwtAttempt,
    ApiJwtContext,
    ApiJwtInput,
    api_jwt_request,
    api_jwt_settings_compatible,
    api_jwt_user,
    effective_api_jwt_key,
    private_api_jwt_exception,
    require_api_jwt_result,
    resolve_api_jwt_request,
)
from litellm.proxy.retail.api_jwt_config import ApiJwtFailure
from litellm.proxy.retail.api_jwt_store import ResolvedApiJwt

_PRIVATE: Final = "private-access-jwt-subject-jti-marker"
_HASH: Final = "a" * 64


@pytest.fixture(autouse=True)
def healthy_native_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.proxy.retail import api_jwt_auth

    monkeypatch.setattr(api_jwt_auth, "api_jwt_compatible", lambda: True)
    monkeypatch.setattr(api_jwt_auth, "coordination_guard_status", lambda: SimpleNamespace(accepting_credentials=True))


def request(context: ApiJwtInput | ApiJwtContext | ApiJwtAttempt | None = None) -> Request:
    scope: Final = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    return Request({**scope, **({API_JWT_SCOPE_KEY: context} if context is not None else {})})


def resolved() -> ResolvedApiJwt:
    user: Final = LiteLLM_UserTable(user_id="bound-user", user_role="customer", max_budget=7, rpm_limit=11)
    auth: Final = UserAPIKeyAuth(
        api_key=_HASH, token=_HASH, user_id=user.user_id, user_role=user.user_role,
        models=["allowed-model"], key_type="llm_api", metadata={"retail_resource_pool_ids": ["allowed-pool"]},
    )
    return ResolvedApiJwt(auth=auth, user=user, target_hash=_HASH)


class Resolver:
    def __init__(self, outcome: ResolvedApiJwt | ApiJwtFailure | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self, credential: ApiJwtInput) -> ResolvedApiJwt | ApiJwtFailure:
        self.calls += 1
        assert credential.token.get_secret_value() == _PRIVATE
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.asyncio
async def test_ordinary_key_preserves_identity_and_never_resolves_external_identity() -> None:
    reader: Final = Resolver(RuntimeError("external lookup must not run"))
    ordinary: Final = request()
    assert await resolve_api_jwt_request(ordinary, "sk-ordinary", resolve=reader) is None
    assert reader.calls == 0
    assert effective_api_jwt_key(ordinary, "sk-ordinary") == "sk-ordinary"
    assert api_jwt_user(ordinary) is None and not api_jwt_request(ordinary)


@pytest.mark.asyncio
async def test_verified_mapping_consumes_secret_and_keeps_native_authority_and_limits() -> None:
    target: Final = resolved()
    selected: Final = request(ApiJwtInput(SecretStr(_PRIVATE)))
    reader: Final = Resolver(target)
    assert await resolve_api_jwt_request(selected, None, resolve=reader) is target
    assert reader.calls == 1
    assert api_jwt_user(selected) is target.user
    assert effective_api_jwt_key(selected, None) == target.target_hash
    assert _PRIVATE not in repr(selected.scope)
    assert isinstance(selected.scope[API_JWT_SCOPE_KEY], ApiJwtContext)
    require_api_jwt_result(selected, target.auth)
    assert target.auth.models == ["allowed-model"]
    assert target.auth.metadata == {"retail_resource_pool_ids": ["allowed-pool"]}
    assert target.auth.jwt_claims is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status", (
    (ApiJwtFailure("invalid"), 401), (ApiJwtFailure("unavailable"), 503), (RuntimeError(_PRIVATE), 503),
))
async def test_failures_are_private_and_cannot_leave_reusable_input(
    outcome: ApiJwtFailure | Exception, status: int,
) -> None:
    selected: Final = request(ApiJwtInput(SecretStr(_PRIVATE)))
    reader: Final = Resolver(outcome)
    with pytest.raises(HTTPException) as error:
        await resolve_api_jwt_request(selected, None, resolve=reader)
    assert error.value.status_code == status
    assert _PRIVATE not in str(error.value)
    assert isinstance(selected.scope[API_JWT_SCOPE_KEY], ApiJwtAttempt)
    assert _PRIVATE not in repr(selected.scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ("rt-jwt.redacted", "Bearer rt-jwt.private", "BEARER RT-JWT.private"))
async def test_missing_sanitizer_context_never_falls_into_native_custom_or_dev_auth(credential: str) -> None:
    reader: Final = Resolver(resolved())
    with pytest.raises(HTTPException) as error:
        await resolve_api_jwt_request(request(), credential, resolve=reader)
    assert error.value.status_code == 401 and reader.calls == 0


@pytest.mark.parametrize("change", (
    {"user_role": "proxy_admin"}, {"user_id": "different-owner"}, {"api_key": "b" * 64},
    {"token": "b" * 64}, {"jwt_claims": {"sub": _PRIVATE}}, {"team_id": "other-team"},
    {"org_id": "other-org"}, {"project_id": "other-project"}, {"agent_id": "other-agent"},
    {"is_session_token": True},
))
def test_final_native_projection_cannot_promote_role_or_change_funding_identity(change: Mapping[str, object]) -> None:
    target: Final = resolved()
    selected: Final = request(ApiJwtContext(target.user, target.target_hash))
    with pytest.raises(HTTPException) as error:
        require_api_jwt_result(selected, target.auth.model_copy(update=change))
    assert error.value.status_code == 401 and _PRIVATE not in str(error.value)


def test_attempt_without_resolved_authority_cannot_be_authorized() -> None:
    with pytest.raises(HTTPException):
        require_api_jwt_result(request(ApiJwtAttempt()), resolved().auth)


@pytest.mark.parametrize("error,status", (
    (HTTPException(401, _PRIVATE), 401), (HTTPException(402, _PRIVATE), 402),
    (HTTPException(403, _PRIVATE), 403), (HTTPException(429, _PRIVATE), 429),
    (HTTPException(503, _PRIVATE), 503), (RuntimeError(_PRIVATE), 401), (TimeoutError(_PRIVATE), 503),
))
def test_private_error_projection_keeps_retry_status_without_token_or_claims(error: Exception, status: int) -> None:
    projected: Final = private_api_jwt_exception(error, request(ApiJwtAttempt()))
    assert isinstance(projected, ProxyException) and projected.code == str(status)
    assert _PRIVATE not in str(projected.to_dict())
    assert projected.param is None
    assert private_api_jwt_exception(error, request(), "sk-ordinary") is error


@pytest.mark.parametrize("setting", (
    "enable_jwt_auth", "enable_oauth2_auth", "enable_oauth2_proxy_auth", "litellm_key_header_name",
))
def test_incompatible_auth_configuration_is_rejected_without_changing_other_settings(setting: str) -> None:
    assert not api_jwt_settings_compatible({setting: True})
    assert api_jwt_settings_compatible({setting: False, "unrelated": object()})


@pytest.mark.parametrize("boundary", ("callback", "coordination"))
def test_final_admission_rechecks_current_billing_and_coordination_health(
    boundary: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy.retail import api_jwt_auth

    if boundary == "callback":
        monkeypatch.setattr(api_jwt_auth, "api_jwt_compatible", lambda: False)
    else:
        monkeypatch.setattr(api_jwt_auth, "coordination_guard_status", lambda: SimpleNamespace(accepting_credentials=False))
    target: Final = resolved()
    with pytest.raises(HTTPException) as failure:
        require_api_jwt_result(request(ApiJwtContext(target.user, target.target_hash)), target.auth)
    assert failure.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ("admin", "master", "ordinary"))
async def test_native_mapping_rejects_configured_admin_and_master_credentials(
    collision: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy import proxy_server
    from litellm.proxy.retail import api_jwt_auth, api_jwt_service
    from litellm.proxy.retail.api_jwt_provider import VerifiedApiJwt
    from litellm.proxy.utils import hash_token

    target: Final = resolved()
    selected: Final = ResolvedApiJwt(
        target.auth, target.user,
        hash_token("sk-fixture-master") if collision == "master" else target.target_hash,
    )
    verification: Final = VerifiedApiJwt(
        SecretStr("fixture-subject"), SecretStr("fixture-client"), UUID(int=1),
        int(datetime.now(timezone.utc).timestamp()), datetime.now(timezone.utc) + timedelta(seconds=100),
    )
    service: Final = SimpleNamespace(
        config=object(), runtime=SimpleNamespace(verify=AsyncMock(return_value=verification)),
        store=SimpleNamespace(fresh=AsyncMock(return_value=selected)),
    )
    monkeypatch.setattr(api_jwt_service, "get_api_jwt_service", lambda: service)
    monkeypatch.setattr(proxy_server, "master_key", "sk-fixture-master")
    monkeypatch.setattr(proxy_server, "litellm_proxy_admin_name", target.user.user_id if collision == "admin" else "configured-admin")
    outcome: Final = await api_jwt_auth._resolve_input(ApiJwtInput(SecretStr(_PRIVATE)))
    assert outcome == ApiJwtFailure("invalid") if collision != "ordinary" else outcome is selected
    service.runtime.verify.assert_awaited_once()
    service.store.fresh.assert_awaited_once()

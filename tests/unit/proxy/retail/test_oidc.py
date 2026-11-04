from dataclasses import replace
from datetime import datetime, timezone
from typing import Final, Literal

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr

from litellm.proxy.retail.oidc import router
from litellm.proxy.retail.oidc_config import OidcConfig, OidcFailure
from litellm.proxy.retail.oidc_http import CALLBACK_PATH, SECURE_FLOW_COOKIE
from litellm.proxy.retail.oidc_runtime import OidcBegin, OidcIdentity
from litellm.proxy.retail.oidc_service import get_oidc_service
from litellm.proxy.retail.oidc_sessions import MintedOIDCSession


def config() -> OidcConfig:
    return OidcConfig.model_validate({
        "namespace": "route-qa", "generation": 2, "issuer": "https://idp.test",
        "client_id": "private-client", "client_auth_method": "client_secret_basic",
        "public_origin": "https://gateway.test",
        "bindings": ({"subject": "private-subject", "user_id": "existing-user"},),
    })


class RuntimeBoundary:
    def __init__(self, settings: OidcConfig) -> None:
        self.begun: OidcBegin | OidcFailure = OidcBegin("https://idp.test/authorize?state=fixture-state", SecretStr("fixture-flow"))
        self.completed: OidcIdentity | OidcFailure = OidcIdentity(
            SecretStr("private-subject"), "existing-user", "/ui/models/?search=public", settings.digest(),
            settings.generation, settings.namespace,
        )
        self.begins: tuple[str, ...] = ()
        self.callbacks: tuple[tuple[str, str | None, str, str | None, str | None], ...] = ()

    async def begin(self, return_to: str = "/ui/") -> OidcBegin | OidcFailure:
        self.begins += (return_to,)
        return self.begun

    async def complete(
        self, *, state: str, code: str | None, cookie: str, issuer: str | None, error: str | None,
    ) -> OidcIdentity | OidcFailure:
        self.callbacks += ((state, code, cookie, issuer, error),)
        return self.completed


class ServiceBoundary:
    def __init__(self) -> None:
        self.config = config()
        self.runtime = RuntimeBoundary(self.config)
        self.mints: tuple[str, ...] = ()
        self.mint_error: HTTPException | RuntimeError | None = None

    async def mint(self, subject: str) -> MintedOIDCSession:
        self.mints += (subject,)
        if self.mint_error is not None:
            raise self.mint_error
        return MintedOIDCSession(jwt_token="fixture-native-jwt", expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc))


def application(service: ServiceBoundary | OidcFailure) -> FastAPI:
    app: Final = FastAPI()
    app.include_router(router)

    def dependency() -> ServiceBoundary | OidcFailure:
        return service

    app.dependency_overrides = {get_oidc_service: dependency}
    return app


def assert_private(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["pragma"] == "no-cache"


def assert_failed(response: httpx.Response, code: str = "failed") -> None:
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/login/?oidc_error={code}"
    assert "set-cookie" not in response.headers
    assert "private-" not in response.text + response.headers["location"]
    assert_private(response)


@pytest.mark.asyncio
async def test_status_is_anonymous_safe_projection_and_does_not_start_a_flow() -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get("/retail/auth/oidc/status")
    assert response.status_code == 200
    assert frozenset(response.json()) == frozenset({"enabled", "available", "label"})
    assert isinstance(response.json()["available"], bool)
    assert "private-" not in response.text
    assert "set-cookie" not in response.headers
    assert service.runtime.begins == ()
    assert service.mints == ()
    assert_private(response)


@pytest.mark.asyncio
async def test_status_unknown_query_is_fixed_private_error_without_authentication_side_effects() -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get("/retail/auth/oidc/status?unknown=private-value")
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "private-value" not in response.text
    assert "set-cookie" not in response.headers
    assert service.runtime.begins == ()
    assert_private(response)


@pytest.mark.asyncio
async def test_start_sets_host_scoped_http_only_cookie_without_modifying_native_cookie() -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get("/retail/auth/oidc/start?return_to=%2Fui%2Fmodels", headers={"cookie": "token=prior-native"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://idp.test/authorize?")
    cookies: Final = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert cookies[0].startswith(f"{SECURE_FLOW_COOKIE}=fixture-flow;")
    assert "Path=/;" in cookies[0]
    assert "HttpOnly" in cookies[0] and "Secure" in cookies[0] and "Domain=" not in cookies[0]
    assert service.runtime.begins == ("/ui/models",)
    assert service.mints == ()
    assert_private(response)


@pytest.mark.parametrize("path", (
    "/start?return_to=/ui&return_to=/ui/models", "/start?unknown=private-input",
    "/callback?state=one&state=two&code=private-code", "/callback?state=one&code=private-code&unknown=private-input",
    "/callback?state=%0a&code=private-code",
))
@pytest.mark.asyncio
async def test_invalid_queries_never_enter_protocol_or_change_cookies(path: str) -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get("/retail/auth/oidc" + path, headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound; token=prior"})
    assert_failed(response)
    assert service.runtime.begins == ()
    assert service.runtime.callbacks == ()
    assert service.mints == ()


@pytest.mark.parametrize("path", ("/start", "/callback?state=one&code=private-code"))
@pytest.mark.asyncio
async def test_duplicate_browser_cookie_is_rejected_without_cancelling_valid_flow(path: str) -> None:
    service: Final = ServiceBoundary()
    headers: Final = (("cookie", f"{SECURE_FLOW_COOKIE}=first"), ("cookie", f"{SECURE_FLOW_COOKIE}=second; token=prior"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get("/retail/auth/oidc" + path, headers=headers)
    assert_failed(response)
    assert service.runtime.begins == ()
    assert service.runtime.callbacks == ()


@pytest.mark.asyncio
async def test_https_callback_does_not_accept_an_unprotected_legacy_cookie() -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(CALLBACK_PATH + "?state=one&code=private-code", headers={"cookie": "retail_oidc_flow=bound; token=prior"})
    assert_failed(response)
    assert service.runtime.callbacks == ()
    assert service.mints == ()


@pytest.mark.parametrize("origin", ("http://gateway.test", "https://other.test"))
@pytest.mark.asyncio
async def test_host_or_forwarded_headers_cannot_downgrade_configured_https_cookie(origin: str) -> None:
    service: Final = ServiceBoundary()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=origin) as client:
        response: Final = await client.get("/retail/auth/oidc/start", headers={"forwarded": "proto=https;host=gateway.test"})
    assert_failed(response)
    assert service.runtime.begins == ()


@pytest.mark.parametrize("path", ("/start", "/callback?state=one&code=private-code"))
@pytest.mark.asyncio
async def test_unavailable_optional_service_preserves_existing_login_cookies(path: str) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(OidcFailure("unavailable"))), base_url="https://gateway.test") as client:
        response: Final = await client.get("/retail/auth/oidc" + path, headers={"cookie": "token=prior; retail_oidc_flow=bound"})
    assert_failed(response, "unavailable")


@pytest.mark.parametrize("code", ("invalid", "unavailable", "denied"))
@pytest.mark.asyncio
async def test_protocol_failures_have_only_fixed_error_codes_and_leave_unbound_cookie_alone(
    code: Literal["invalid", "unavailable", "denied"],
) -> None:
    service: Final = ServiceBoundary()
    service.runtime.completed = OidcFailure(code)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(
            CALLBACK_PATH + "?state=one&error=access_denied&error_description=private-provider-description&error_uri=https://private-idp.test",
            headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound; token=prior"},
        )
    assert_failed(response, "failed" if code == "invalid" else code)
    assert service.runtime.callbacks == (("one", None, "bound", None, "access_denied"),)
    assert service.mints == ()


@pytest.mark.parametrize("target", (
    "https://other.test/ui", "//other.test/ui", "/outside", "/ui/login", "/ui/../outside", "/ui?redirect_to=private-url",
))
@pytest.mark.asyncio
async def test_callback_revalidates_identity_return_target_before_mint(target: str) -> None:
    service: Final = ServiceBoundary()
    identity: Final = service.runtime.completed
    assert isinstance(identity, OidcIdentity)
    service.runtime.completed = replace(identity, return_to=target)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(CALLBACK_PATH + "?state=one&code=private-code", headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound"})
    assert_failed(response)
    assert service.mints == ()


@pytest.mark.parametrize("mismatch", ("namespace", "generation", "config_digest"))
@pytest.mark.asyncio
async def test_callback_refuses_identity_from_another_configuration_generation(mismatch: str) -> None:
    service: Final = ServiceBoundary()
    identity: Final = service.runtime.completed
    assert isinstance(identity, OidcIdentity)
    if mismatch == "namespace":
        service.runtime.completed = replace(identity, namespace="different")
    elif mismatch == "generation":
        service.runtime.completed = replace(identity, generation=identity.generation + 1)
    else:
        service.runtime.completed = replace(identity, config_digest="different")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(CALLBACK_PATH + "?state=one&code=private-code", headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound"})
    assert_failed(response)
    assert service.mints == ()


@pytest.mark.parametrize("error", (HTTPException(403, "private-authority"), HTTPException(503, "private-storage"), RuntimeError("private-storage")))
@pytest.mark.asyncio
async def test_mint_failure_cannot_replace_native_cookie_or_expose_private_error(error: HTTPException | RuntimeError) -> None:
    service: Final = ServiceBoundary()
    service.mint_error = error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(CALLBACK_PATH + "?state=one&code=private-code", headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound; token=prior"})
    assert_failed(response, "denied" if isinstance(error, HTTPException) and error.status_code == 403 else "unavailable")


@pytest.mark.asyncio
async def test_successful_callback_normalizes_target_and_sets_only_compatible_session_and_flow_cleanup() -> None:
    service: Final = ServiceBoundary()
    identity: Final = service.runtime.completed
    assert isinstance(identity, OidcIdentity)
    service.runtime.completed = replace(identity, return_to="https://gateway.test/ui/models/?search=public")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(service)), base_url=service.config.public_origin) as client:
        response: Final = await client.get(CALLBACK_PATH + "?state=one&code=private-code&iss=https%3A%2F%2Fidp.test", headers={"cookie": f"{SECURE_FLOW_COOKIE}=bound"})
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/models/?search=public"
    assert "private-" not in response.headers["location"]
    cookies: Final = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert cookies[0].startswith("token=fixture-native-jwt;") and "HttpOnly" not in cookies[0]
    assert "Secure" in cookies[0] and "Path=/;" in cookies[0]
    assert cookies[1].startswith(f'{SECURE_FLOW_COOKIE}="";') and "Max-Age=0" in cookies[1]
    assert "Path=/;" in cookies[1] and "Domain=" not in cookies[1]
    assert service.mints == ("private-subject",)
    assert_private(response)

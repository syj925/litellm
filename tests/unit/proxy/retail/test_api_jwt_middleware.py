import io
import logging
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, Request, Security, WebSocket
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from litellm.proxy.retail.api_jwt_auth import API_JWT_SCOPE_KEY, ApiJwtInput
from litellm.proxy.retail.api_jwt_middleware import ApiJwtAccessLogFilter, ApiJwtCredentialMiddleware
from litellm.types.proxy.auth.special_headers import SpecialHeaders

_TOKEN: Final = "fixture-private-access-jwt-subject-jti"
_WRAPPED: Final = "rt-jwt." + _TOKEN


class Probe:
    def __init__(self) -> None:
        self.calls = 0
        self.credential_names: tuple[str, ...] = ()
        self.private_input = False

    def app(self) -> FastAPI:
        app: Final = FastAPI()
        app.add_middleware(ApiJwtCredentialMiddleware)
        security: Final = APIKeyHeader(name="Authorization", auto_error=False)

        @app.post("/v1/chat/completions")
        async def invoke(request: Request, key: str | None = Security(security)) -> Mapping[str, object]:
            self.calls += 1
            self.credential_names = tuple(name for name in request.headers if name in SpecialHeaders.litellm_credential_header_names())
            credential: Final = request.scope.get(API_JWT_SCOPE_KEY)
            self.private_input = isinstance(credential, ApiJwtInput)
            if isinstance(credential, ApiJwtInput):
                assert credential.token.get_secret_value() == _TOKEN
                assert key is None and _TOKEN not in repr(request.scope)
            return {"called": True, "ordinary_key": key, "header": request.headers.get("x-unrelated")}

        @app.websocket("/v1/realtime")
        async def realtime(socket: WebSocket) -> None:
            self.calls += 1
            await socket.accept()
            await socket.send_text("endpoint reached")

        return app


@pytest.mark.asyncio
@pytest.mark.parametrize("name", tuple(SpecialHeaders.litellm_credential_header_names()))
async def test_real_security_dependency_receives_no_jwt_or_forwardable_credential_header(name: str) -> None:
    probe: Final = Probe()
    transport: Final = httpx.ASGITransport(app=probe.app())
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        result: Final = await client.post(
            "/v1/chat/completions", headers={name: "Bearer " + _WRAPPED, "X-Unrelated": "preserved"}, json={},
        )
    assert result.status_code == 200 and result.json()["header"] == "preserved"
    assert probe.calls == 1 and probe.private_input and probe.credential_names == ()
    assert _TOKEN not in result.text


@pytest.mark.asyncio
async def test_ordinary_header_is_unchanged_and_no_private_context_is_created() -> None:
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        result: Final = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-normal"}, json={})
    assert result.status_code == 200 and result.json()["ordinary_key"] == "Bearer sk-normal"
    assert probe.calls == 1 and not probe.private_input and probe.credential_names == ("authorization",)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", (
    (("Authorization", "Bearer " + _WRAPPED),), (("x-api-key", "sk-other"),),
    (("Cookie", "token=old-ui-token"),), (("x-litellm-disable-callbacks", "RetailBilling"),),
    (("x-mcp-auth", "other-token"),),
))
async def test_ambiguous_or_callback_disabling_credentials_stop_before_endpoint(extra: tuple[tuple[str, str], ...]) -> None:
    probe: Final = Probe()
    headers: Final = (("Authorization", "Bearer " + _WRAPPED), *extra)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        result: Final = await client.post("/v1/chat/completions", headers=headers, json={})
    assert result.status_code == 401 and probe.calls == 0
    assert _TOKEN not in result.text and "no-store" in result.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", (
    ("/retail/wallet", "GET"), ("/key/generate", "POST"), ("/v1/models", "GET"),
    ("/mcp", "POST"), ("/v1/realtime", "GET"), ("/v1/batches", "POST"),
    ("/v1/chat/completions", "GET"), ("/v1/embeddings", "POST"),
))
async def test_non_inference_routes_fail_before_downstream_auth_or_business_side_effect(path: str, method: str) -> None:
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        result: Final = await client.request(method, path, headers={"Authorization": "Bearer " + _WRAPPED})
    assert result.status_code == 401 and probe.calls == 0 and _TOKEN not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ("api_key=" + _WRAPPED, "access_token=" + quote(_WRAPPED), "key=sk-other"))
async def test_credentials_in_query_never_reach_endpoint(query: str) -> None:
    probe: Final = Probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe.app()), base_url="https://gateway.test") as client:
        result: Final = await client.post("/v1/chat/completions?" + query, headers={"Authorization": "Bearer " + _WRAPPED}, json={})
    assert result.status_code == 401 and probe.calls == 0 and _TOKEN not in result.text


@pytest.mark.parametrize("target", (
    "/v1/chat/completions?api_key=" + _WRAPPED,
    "/v1/chat/completions?access_token=" + quote(_WRAPPED),
    "/" + _WRAPPED,
))
def test_actual_logging_handler_does_not_emit_wrapped_credentials(target: str) -> None:
    output: Final = io.StringIO()
    logger: Final = logging.Logger("isolated-access")
    logger.addHandler(logging.StreamHandler(output))
    logger.addFilter(ApiJwtAccessLogFilter())
    logger.info('%s - "%s %s HTTP/%s" %d', "fixture-peer", "POST", target, "1.1", 401)
    assert _TOKEN not in output.getvalue() and _WRAPPED not in output.getvalue()
    logger.info('%s - "%s %s HTTP/%s" %d', "fixture-peer", "GET", "/ui/models/?search=public", "1.1", 200)
    assert "/ui/models/?search=public" in output.getvalue()


def test_reserved_namespace_cannot_open_a_websocket_or_reach_realtime_endpoint() -> None:
    probe: Final = Probe()
    with (
        TestClient(probe.app()) as client,
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect("/v1/realtime", headers={"Authorization": "Bearer " + _WRAPPED}),
    ):
        pytest.fail("reserved API access token opened a websocket")
    assert rejected.value.code == 1008 and probe.calls == 0
    assert _TOKEN not in rejected.value.reason


@pytest.mark.asyncio
async def test_configured_root_path_is_stripped_before_exact_native_route_admission() -> None:
    probe: Final = Probe()
    transport: Final = httpx.ASGITransport(app=probe.app(), root_path="/gateway")
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response: Final = await client.post("/gateway/v1/chat/completions", headers={"Authorization": "Bearer " + _WRAPPED}, json={})
    assert response.status_code == 200 and probe.calls == 1 and _TOKEN not in response.text

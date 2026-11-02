import gzip
import json
from collections.abc import Sequence
from typing import Final

import httpx
import pytest
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.proxy.auth.auth_method import AuthMethod
from litellm.proxy.auth.resolvers.models import Principal, PrincipalType
from litellm.proxy.auth.roles import Role
from litellm.proxy.retail.customer_response_middleware import CustomerResponsePrivacyMiddleware

_PRIVATE: Final = "https://supplier.invalid/private/deployment/sk-fixture-private-value"
_ERROR: Final = json.dumps({"error": {"message": _PRIVATE, "provider_specific_fields": {"cost": 91}}}).encode()
_HEADERS: Final = (
    (b"x-litellm-model-api-base", _PRIVATE.encode()), (b"x-litellm-response-cost-original", b"91"),
    (b"x-request-id", b"upstream-private-request"), (b"x-supplier-token", b"sk-fixture-private-value"),
)


def response_app(
    chunks: Sequence[bytes], *, status: int = 503, content_type: bytes = b"application/json",
    role: Role | None = None, encoding: bytes | None = None,
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        if role is not None:
            scope.setdefault("state", {})["principal"] = Principal(
                principal_type=PrincipalType.HUMAN, subject="test-user", auth_method=AuthMethod.API_KEY, roles=[role],
            )
        await send({"type": "http.response.start", "status": status, "headers": [
            *_HEADERS, (b"content-type", content_type),
            *([(b"content-encoding", encoding)] if encoding is not None else []),
        ]})
        for index, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": index < len(chunks) - 1})

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (None, Role.ORG_ADMIN, Role.ORG_VIEWER, Role.TEAM_ADMIN, Role.TEAM_MEMBER))
async def test_nonproxy_roles_cannot_obtain_supplier_errors_or_headers(role: Role | None) -> None:
    app: Final = CustomerResponsePrivacyMiddleware(response_app((_ERROR,), role=role))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/chat/completions", json={"metadata": {"role": "proxy_admin"}})
    assert response.status_code == 503
    assert "supplier.invalid" not in response.text and "private-value" not in str(response.headers)
    assert "x-litellm-model-api-base" not in response.headers
    assert "x-litellm-response-cost-original" not in response.headers
    assert response.headers["x-request-id"] != "upstream-private-request"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", (Role.PLATFORM_ADMIN, Role.PLATFORM_VIEWER))
async def test_trusted_proxy_admin_diagnostics_remain_available(role: Role) -> None:
    app: Final = CustomerResponsePrivacyMiddleware(response_app((_ERROR,), role=role))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/messages", json={})
    assert response.status_code == 503 and response.content == _ERROR
    assert response.headers["x-litellm-model-api-base"] == _PRIVATE
    assert response.headers["x-request-id"] == "upstream-private-request"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", (b"gzip", b"unknown", b"deflate"))
async def test_encoded_errors_never_fall_back_to_original_sensitive_bytes(encoding: bytes) -> None:
    compressed: Final = gzip.compress(_ERROR, mtime=0)
    app: Final = CustomerResponsePrivacyMiddleware(response_app((compressed,), encoding=encoding))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/messages", json={})
    assert response.status_code == 503 and "supplier.invalid" not in response.text
    assert response.json()["error"]["message"]
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_fragmented_sse_errors_are_redacted_while_normal_text_is_preserved() -> None:
    normal: Final = b'data: {"choices":[{"delta":{"content":"https://supplier.invalid user text"}}]}\n\n'
    error: Final = b"event: error\ndata: " + _ERROR + b"\n\n"
    chunks: Final = (normal, error[:11], error[11:53], error[53:], b"data: [DONE]\n\n")
    app: Final = CustomerResponsePrivacyMiddleware(response_app(chunks, status=200, content_type=b"text/event-stream"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/chat/completions", json={})
    assert response.status_code == 200 and response.content.startswith(normal)
    assert _PRIVATE not in response.text and "private-value" not in response.text
    assert response.content.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_compressed_sse_is_decoded_incrementally_and_errors_are_sanitized() -> None:
    normal: Final = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    compressed: Final = gzip.compress(normal + b"event: error\ndata: " + _ERROR + b"\n\n", mtime=0)
    app: Final = CustomerResponsePrivacyMiddleware(response_app(
        (compressed[:17], compressed[17:]), status=200, content_type=b"text/event-stream", encoding=b"gzip",
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/responses", json={})
    assert response.status_code == 200 and response.content.startswith(normal)
    assert _PRIVATE not in response.text and "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_success_envelope_privacy_does_not_rewrite_model_content_or_tool_arguments() -> None:
    document: Final = {
        "choices": [{"message": {"content": _PRIVATE, "tool_calls": [{"function": {"arguments": _PRIVATE}}]}}],
        "usage": {"prompt_tokens": 2}, "provider_specific_fields": {"supplier_cost": 91},
    }
    app: Final = CustomerResponsePrivacyMiddleware(response_app((json.dumps(document).encode(),), status=200))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/chat/completions", json={})
    result: Final = response.json()
    assert result == {"choices": document["choices"], "usage": document["usage"]}


@pytest.mark.asyncio
async def test_noninference_success_body_is_unchanged() -> None:
    app: Final = CustomerResponsePrivacyMiddleware(response_app((_ERROR,), status=200))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/retail/ledger")
    assert response.content == _ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (200, 503))
async def test_log_responses_are_not_cached_and_error_diagnostics_are_still_filtered(status: int) -> None:
    app: Final = CustomerResponsePrivacyMiddleware(response_app((_ERROR,), status=status))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/spend/logs")
    assert response.headers["cache-control"] == "no-store"
    assert (_PRIVATE in response.text) is (status == 200)

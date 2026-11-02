import gzip
import json
import tracemalloc
import zlib
from collections.abc import Sequence
from typing import Final

import httpx
import pytest
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from litellm.proxy.auth.auth_method import AuthMethod
from litellm.proxy.auth.resolvers.models import Principal, PrincipalType
from litellm.proxy.auth.roles import Role
from litellm.proxy.retail.customer_response_middleware import CustomerResponsePrivacyMiddleware, _CustomerResponse

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


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", (None, b"gzip", b"deflate"))
async def test_large_fragmented_utf8_json_preserves_complete_wire_body_for_each_supported_encoding(
    encoding: bytes | None,
) -> None:
    document: Final = {
        "choices": [{"message": {"role": "assistant", "content": "你好🌏" * 100_000 + _PRIVATE}}],
        "metadata": {"customer_label": "保留"},
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }
    original: Final = json.dumps(document, ensure_ascii=False).encode("utf-8")
    compressed: Final = (
        gzip.compress(original, mtime=0)
        if encoding == b"gzip"
        else (zlib.compress(original) if encoding == b"deflate" else original)
    )
    chunks: Final = tuple(compressed[index : index + 1021] for index in range(0, len(compressed), 1021))
    assert len(original) > 1_000_000 and len(chunks) > 1
    app: Final = CustomerResponsePrivacyMiddleware(response_app(chunks, status=200, encoding=encoding))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/chat/completions", json={})
    assert response.status_code == 200 and response.content == original
    assert "content-encoding" not in response.headers

class _Sent:
    def __init__(self) -> None:
        self.count = 0
        self.status: int | None = None
        self.body = b""

    async def __call__(self, message: Message) -> None:
        self.count += 1
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            body: Final = message.get("body", b"")
            assert isinstance(body, bytes)
            self.body += body


@pytest.mark.asyncio
async def test_multipart_large_json_waits_for_final_body_and_applies_the_existing_projection() -> None:
    public: Final = {"choices": [{"message": {"content": "x" * (1024 * 1024)}}], "usage": {"total_tokens": 5}}
    source: Final = json.dumps({**public, "provider_cost": 123, "model_info": {"supplier": _PRIVATE}}).encode()
    sent: Final = _Sent()
    scope: Final[Scope] = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    response: Final = _CustomerResponse(scope, sent)
    await response.send(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]}
    )
    assert sent.count == 0
    for offset in range(0, len(source), 1024):
        final: Final = offset + 1024 >= len(source)
        await response.send(
            {"type": "http.response.body", "body": source[offset : offset + 1024], "more_body": not final}
        )
        if not final:
            assert sent.count == 0
    assert sent.count == 2 and sent.status == 200
    assert json.loads(sent.body) == public


@pytest.mark.asyncio
async def test_body_limit_discards_accumulated_json_and_retains_no_later_large_fragments() -> None:
    sent: Final = _Sent()
    scope: Final[Scope] = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []}
    response: Final = _CustomerResponse(scope, sent)
    await response.send(
        {"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]}
    )
    fragment: Final = _PRIVATE.encode() + b"x" * (1024 * 1024)
    was_tracing: Final = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        baseline: Final = tracemalloc.get_traced_memory()[0]
        await response.send({"type": "http.response.body", "body": b"x" * 65_536, "more_body": True})
        for _ in range(20):
            await response.send({"type": "http.response.body", "body": fragment, "more_body": True})
        retained: Final = tracemalloc.get_traced_memory()[0] - baseline
        assert retained < 256 * 1024
        assert sent.count == 0
        await response.send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        if not was_tracing:
            tracemalloc.stop()
    assert sent.count == 2 and sent.status == 503
    assert _PRIVATE.encode() not in sent.body and json.loads(sent.body)["error"]["message"]


@pytest.mark.asyncio
async def test_one_public_final_json_chunk_reuses_the_original_wire_bytes() -> None:
    original: Final = json.dumps(
        {"choices": [{"message": {"content": "public text"}}], "usage": {"total_tokens": 3}}
    ).encode()
    sent: Final = _Sent()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            assert message["body"] is original
        await sent(message)

    scope: Final[Scope] = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    response: Final = _CustomerResponse(scope, send)
    await response.send(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]}
    )
    assert sent.count == 0
    await response.send({"type": "http.response.body", "body": original, "more_body": False})
    assert sent.count == 2 and sent.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", (None, b"gzip", b"deflate"))
async def test_single_final_error_chunk_still_enforces_wire_and_decoded_limits(encoding: bytes | None) -> None:
    original: Final = json.dumps({"error": {"message": _PRIVATE + "x" * 70_000}}).encode()
    wire: Final = gzip.compress(original, mtime=0) if encoding == b"gzip" else (
        zlib.compress(original) if encoding == b"deflate" else original
    )
    app: Final = CustomerResponsePrivacyMiddleware(response_app((wire,), encoding=encoding))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/messages", json={})
    assert response.status_code == 503 and len(response.content) < 65_536
    assert _PRIVATE not in response.text and "content-encoding" not in response.headers

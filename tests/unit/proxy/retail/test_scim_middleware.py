import logging
from typing import Final

import httpx
import pytest
from starlette.types import Receive, Scope, Send

from litellm.proxy.retail.scim_middleware import (
    SCIM_SCOPE_KEY,
    ScimAccessLogFilter,
    ScimCredential,
    ScimIngressMiddleware,
)


class CaptureApp:
    def __init__(self) -> None:
        self.calls = 0
        self.headers: tuple[tuple[bytes, bytes], ...] = ()
        self.credential: ScimCredential | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.headers = tuple(scope.get("headers", ()))
        captured: Final = scope.get(SCIM_SCOPE_KEY)
        self.credential = captured if isinstance(captured, ScimCredential) else None
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"captured":true}'})


@pytest.mark.asyncio
async def test_directory_app_receives_private_credential_without_authorization_or_raw_token_headers() -> None:
    native: Final = CaptureApp()
    directory: Final = CaptureApp()
    token: Final = "rt-scim."+"f"*32
    app: Final = ScimIngressMiddleware(native, directory_app=directory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.invalid") as client:
        response: Final = await client.get("/retail/scim/v2/Users", headers={
            "Authorization": "Bearer "+token, "x-private-note": token, "x-trace-probe": "visible-nonsecret",
        })
    assert response.status_code == 200 and directory.calls == 1 and native.calls == 0
    assert directory.credential is not None and directory.credential.token.get_secret_value() == token
    assert all(name.lower() != b"authorization" and token.encode() not in value for name, value in directory.headers)
    assert (b"x-trace-probe", b"visible-nonsecret") in directory.headers


@pytest.mark.parametrize("target", (
    "/retail/scim/v2/Users?filter=userName+eq+private-name",
    "/prefix/retail/scim/v2/Users/private-name?displayName=private-name",
    "/prefix/retail%2Fscim%2Fv2/Users/private-name",
))
def test_directory_access_logs_redact_profile_values_in_paths_and_queries(target: str) -> None:
    args: Final = ("client", "GET", target, "1.1", 404)
    record: Final = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1, "%s %s %s %s %s", args, None)
    assert ScimAccessLogFilter().filter(record)
    assert "private-name" not in record.getMessage()
    assert record.args == ("client", "GET", "/retail/scim/v2/[resource]", "1.1", 404)


@pytest.mark.parametrize("target", ("/rt-scim.private-secret", "/rt%2dscim.private-secret", "/v1/models?key=rt-scim.private-secret"))
def test_directory_tokens_in_uris_never_appear_in_access_records(target: str) -> None:
    record: Final = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1, "%s %s %s %s %s", ("client", "GET", target, "1.1", 401), None)
    assert ScimAccessLogFilter().filter(record)
    assert "private-secret" not in record.getMessage()


def test_non_directory_access_records_keep_the_original_target() -> None:
    args: Final = ("client", "GET", "/v1/models?limit=10", "1.1", 200)
    record: Final = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1, "%s %s %s %s %s", args, None)
    assert ScimAccessLogFilter().filter(record) and record.args is args

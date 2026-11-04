import asyncio
import base64
import hmac
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from hashlib import sha256
from typing import Final
from uuid import UUID

import httpx
import pytest

from litellm.proxy.retail.audit_delivery_config import WebhookConfig
from litellm.proxy.retail.audit_delivery_store import DeliveryClaim, DeliveryEnvelope
from litellm.proxy.retail.audit_delivery_worker import DeliveryFailure, DeliverySent, delivery_headers, send_delivery

_KEY: Final = bytes(range(32))
_CONFIG: Final = WebhookConfig("https://receiver.example/events", _KEY, "a" * 64)
_CLAIM: Final = DeliveryClaim(
    id=UUID("00000000-0000-4000-8000-000000000001"),
    event_id="00000000-0000-4000-8000-000000000002",
    config_fingerprint=_CONFIG.fingerprint,
    lease_token=UUID("00000000-0000-4000-8000-000000000003"),
    lease_owner="fixture-worker",
    attempts=1,
    payload=DeliveryEnvelope(
        event_id=UUID("00000000-0000-4000-8000-000000000002"),
        action="model_budget.created",
        resource_type="model_budget",
        result="succeeded",
        version_before=None,
        version_after=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ),
    lease_until=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
)


class UnreadBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        pytest.fail("Audit delivery must classify response headers without reading its response body")
        yield b""

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sent_bytes_are_signed_and_private_internal_fields_are_not_sent() -> None:
    stream: Final = UnreadBody()

    async def receive(request: httpx.Request) -> httpx.Response:
        payload: Final = request.content
        expected: Final = hmac.new(
            _KEY, f"{_CLAIM.id}.1234.".encode() + payload, sha256
        ).digest()
        assert request.headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode()
        assert request.headers["webhook-id"] == str(_CLAIM.id)
        assert request.headers["webhook-timestamp"] == "1234"
        assert request.method == "POST" and request.url == _CONFIG.url
        assert json.loads(payload) == {
            "event_id": _CLAIM.event_id,
            "action": "model_budget.created",
            "resource_type": "model_budget",
            "result": "succeeded",
            "version_before": None,
            "version_after": 1,
            "occurred_at": "2026-01-01T00:00:00Z",
        }
        return httpx.Response(204, stream=stream, headers={"Content-Length": "999999999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        outcome: Final = await send_delivery(client, _CONFIG, _CLAIM, 1234)
    assert outcome == DeliverySent()
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (200, 201, 202, 204, 299))
async def test_all_success_statuses_close_without_reading_a_body(status: int) -> None:
    stream: Final = UnreadBody()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, stream=stream))
    ) as client:
        outcome: Final = await send_delivery(client, _CONFIG, _CLAIM, 1234)
    assert outcome == DeliverySent()
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    (
        (301, DeliveryFailure("redirect_rejected", False)),
        (302, DeliveryFailure("redirect_rejected", False)),
        (307, DeliveryFailure("redirect_rejected", False)),
        (400, DeliveryFailure("http_rejected", False)),
        (401, DeliveryFailure("http_rejected", False)),
        (403, DeliveryFailure("http_rejected", False)),
        (408, DeliveryFailure("http_rejected", False)),
        (429, DeliveryFailure("http_retryable", True)),
        (500, DeliveryFailure("http_retryable", True)),
        (503, DeliveryFailure("http_retryable", True)),
    ),
)
async def test_failure_statuses_never_follow_redirects_or_read_private_bodies(
    status: int, expected: DeliveryFailure
) -> None:
    stream: Final = UnreadBody()

    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.url == _CONFIG.url
        return httpx.Response(status, stream=stream, headers={"Location": "http://private.example/never-follow"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive), follow_redirects=True) as client:
        outcome: Final = await send_delivery(client, _CONFIG, _CLAIM, 1234)
    assert outcome == expected
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected",
    (
        (httpx.ReadTimeout("private-header-value"), DeliveryFailure("timeout", True)),
        (httpx.ConnectError("private-endpoint-value"), DeliveryFailure("transport_error", True)),
    ),
)
async def test_transport_errors_are_classified_without_echoing_exception_text(
    failure: httpx.RequestError, expected: DeliveryFailure
) -> None:
    async def receive(request: httpx.Request) -> httpx.Response:
        raise failure

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        outcome: Final = await send_delivery(client, _CONFIG, _CLAIM, 1234)
    assert outcome == expected
    assert "private" not in repr(outcome)


def test_retries_keep_the_delivery_id_but_refresh_the_signature_timestamp() -> None:
    first: Final = delivery_headers(_CLAIM.id, 1234, b"{}", _KEY)
    retried: Final = delivery_headers(_CLAIM.id, 1235, b"{}", _KEY)
    assert first["webhook-id"] == retried["webhook-id"] == str(_CLAIM.id)
    assert first["webhook-signature"] != retried["webhook-signature"]
    changed_body: Final = delivery_headers(_CLAIM.id, 1234, b'{"changed":true}', _KEY)
    assert first["webhook-signature"] != changed_body["webhook-signature"]


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_not_converted_to_an_http_failure() -> None:
    async def receive(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(asyncio.CancelledError):
            await send_delivery(client, _CONFIG, _CLAIM, 1234)


@pytest.mark.asyncio
async def test_receiver_cookies_are_neither_sent_nor_retained_for_the_next_delivery() -> None:
    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie", "") == ""
        return httpx.Response(204, headers={"Set-Cookie": "receiver-session=private-receiver-token; Path=/"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), cookies={"existing-session": "private-existing-token"}
    ) as client:
        first: Final = await send_delivery(client, _CONFIG, _CLAIM, 1234)
        assert first == DeliverySent() and not client.cookies
        second: Final = await send_delivery(client, _CONFIG, _CLAIM, 1235)
        assert second == DeliverySent() and not client.cookies


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", (False, True))
async def test_failed_and_cancelled_attempts_also_dispose_cookie_state(cancelled: bool) -> None:
    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie", "") == ""
        if cancelled:
            raise asyncio.CancelledError
        raise httpx.ConnectError("fixture-only-error", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), cookies={"existing-session": "private-existing-token"}
    ) as client:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await send_delivery(client, _CONFIG, _CLAIM, 1234)
        else:
            assert await send_delivery(client, _CONFIG, _CLAIM, 1234) == DeliveryFailure("transport_error", True)
        assert not client.cookies

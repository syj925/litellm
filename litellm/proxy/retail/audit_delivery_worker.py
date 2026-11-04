import asyncio
import base64
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, TypeAlias, assert_never
from uuid import UUID, uuid4

import httpx

from litellm._logging import verbose_proxy_logger

from .audit_delivery_config import WebhookConfig, WebhookConfiguration, WebhookUnavailable, webhook_configuration
from .audit_delivery_store import AuditDeliveryStore, DeliveryClaim, DeliveryErrorCode, get_audit_delivery_store
from .control_plane_flags import audit_delivery_enabled
from .store import WalletStore


@dataclass(frozen=True, slots=True)
class DeliverySent:
    pass


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    code: DeliveryErrorCode
    retryable: bool


DeliveryOutcome: TypeAlias = DeliverySent | DeliveryFailure


def delivery_body(claim: DeliveryClaim) -> bytes:
    return json.dumps(claim.payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def delivery_headers(identifier: UUID, timestamp: int, body: bytes, secret: bytes) -> Mapping[str, str]:
    signed: Final = str(identifier).encode("ascii") + b"." + str(timestamp).encode("ascii") + b"." + body
    signature: Final = base64.b64encode(hmac.new(secret, signed, sha256).digest()).decode("ascii")
    return {
        "Content-Type": "application/json",
        "Cookie": "",
        "webhook-id": str(identifier),
        "webhook-timestamp": str(timestamp),
        "webhook-signature": "v1," + signature,
    }


async def send_delivery(
    client: httpx.AsyncClient, config: WebhookConfig, claim: DeliveryClaim, timestamp: int
) -> DeliveryOutcome:
    body: Final = delivery_body(claim)
    try:
        async with asyncio.timeout(3):
            async with client.stream(
                "POST",
                config.url,
                content=body,
                headers=delivery_headers(claim.id, timestamp, body, config.secret),
                follow_redirects=False,
            ) as response:
                if 200 <= response.status_code < 300:
                    return DeliverySent()
                if 300 <= response.status_code < 400:
                    return DeliveryFailure("redirect_rejected", False)
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    return DeliveryFailure("http_retryable", True)
                return DeliveryFailure("http_rejected", False)
    except (TimeoutError, httpx.TimeoutException):
        return DeliveryFailure("timeout", True)
    except httpx.RequestError:
        return DeliveryFailure("transport_error", True)
    finally:
        client.cookies.clear()


class AuditDeliveryWorker:
    def __init__(
        self,
        store: AuditDeliveryStore,
        *,
        configuration: Callable[[], WebhookConfiguration] = webhook_configuration,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.owner = str(uuid4())
        self._configuration = configuration
        self._clock = clock
        self._client = httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False, verify=True)
        self._closing = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.initialize()
        self._task = asyncio.create_task(self._run(), name="retail-audit-delivery-worker")

    async def run_once(self) -> int:
        if not audit_delivery_enabled():
            return 0
        config: Final = self._configuration()
        if isinstance(config, WebhookUnavailable):
            return 0
        await self.store.maintain()
        claim: Final = await self.store.claim(self.owner, config.fingerprint)
        if claim is None:
            return 0
        outcome: Final = await send_delivery(self._client, config, claim, int(self._clock()))
        match outcome:
            case DeliverySent():
                await self.store.complete(claim)
            case DeliveryFailure(code=code, retryable=retryable):
                await self.store.fail(claim, code, retryable=retryable)
            case _:
                assert_never(outcome)
        return 1

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  # optional delivery failures must stay outside inference and settlement
                verbose_proxy_logger.warning("Retail audit delivery deferred (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        try:
            await self.store.release_claims(self.owner)
        except Exception as exc:  # noqa: BLE001  # durable leases recover when optional shutdown cannot reach its database
            verbose_proxy_logger.warning("Retail audit delivery shutdown deferred (%s)", type(exc).__name__)
        finally:
            await self._client.aclose()
            await self.store.close()


class _WorkerLifecycle:
    def __init__(self) -> None:
        self.current: AuditDeliveryWorker | None = None
        self.lock = asyncio.Lock()


_lifecycle: Final = _WorkerLifecycle()


async def start_audit_delivery_worker(store: WalletStore) -> None:
    async with _lifecycle.lock:
        if _lifecycle.current is not None or not audit_delivery_enabled():
            return
        selected: Final = AuditDeliveryWorker(get_audit_delivery_store(store))
        try:
            await selected.start()
        except Exception:  # noqa: BLE001  # retry startup with the cached store after disposing only this unused HTTP client
            await selected._client.aclose()
            raise
        _lifecycle.current = selected


async def close_audit_delivery_worker() -> None:
    async with _lifecycle.lock:
        selected: Final = _lifecycle.current
        _lifecycle.current = None
    if selected is not None:
        await selected.close()

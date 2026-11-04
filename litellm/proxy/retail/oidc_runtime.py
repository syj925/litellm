import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr
from redis.asyncio import Redis

from .oidc_config import OidcConfig, OidcFailure, safe_return_to
from .oidc_provider import OidcProvider, OidcSnapshot
from .oidc_state import OidcState


class OidcStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool
    available: bool
    label: str


@dataclass(frozen=True, slots=True)
class OidcBegin:
    authorization_url: str = field(repr=False)
    flow_cookie: SecretStr
    kind: Literal["begin"] = "begin"


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    subject: SecretStr
    user_id: str
    return_to: str
    config_digest: str
    generation: int
    namespace: str
    kind: Literal["identity"] = "identity"


def _safe_parameter(value: str, maximum: int) -> bool:
    return bool(value) and len(value) <= maximum and all(33 <= ord(character) <= 126 for character in value)


class OidcRuntime:
    def __init__(
        self, config: OidcConfig, redis: Redis, client_secret: SecretStr,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._state = OidcState(config, redis)
        self._provider = OidcProvider(config, client_secret, transport=transport)
        self._lock = asyncio.Lock()
        self._callbacks = asyncio.Semaphore(4)
        self._task: asyncio.Task[None] | None = None
        self._available = False
        self._valid_until = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        if await self._state.activate():
            await self._refresh()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(5)
            if await self._state.activate():
                await self._refresh()
            else:
                self._available = False

    def status(self) -> OidcStatus:
        return OidcStatus(
            enabled=True, available=self._available and time.time() < self._valid_until, label=self.config.label,
        )

    async def _snapshot(self) -> OidcSnapshot | OidcFailure:
        snapshot: Final = await self._state.snapshot()
        if (
            isinstance(snapshot, OidcSnapshot) and self._provider.validate_snapshot(snapshot)
            and time.time() - 300 < snapshot.fetched_at <= time.time() + 30
        ):
            self._available = True
            self._valid_until = snapshot.fetched_at + 300
            return snapshot
        self._available = False
        return OidcFailure("unavailable")

    async def _refresh(self, *, force: bool = False) -> OidcSnapshot | OidcFailure:
        async with self._lock:
            existing: Final = await self._snapshot()
            if isinstance(existing, OidcSnapshot) and not force and existing.fetched_at + 240 > time.time():
                return existing
            owner: Final = await self._state.lease()
            if isinstance(owner, OidcFailure):
                return existing
            try:
                snapshot: Final = await self._provider.discover()
                if isinstance(snapshot, OidcSnapshot) and await self._state.publish(owner, snapshot):
                    return await self._snapshot()
                return existing
            finally:
                await self._state.unlock(owner)

    async def begin(self, return_to: str = "/ui/") -> OidcBegin | OidcFailure:
        target: Final = safe_return_to(self.config, return_to)
        if isinstance(target, OidcFailure):
            return target
        snapshot: Final = await self._snapshot()
        if isinstance(snapshot, OidcFailure):
            return snapshot
        state: Final = secrets.token_urlsafe(32)
        nonce: Final = secrets.token_urlsafe(32)
        cookie: Final = secrets.token_urlsafe(32)
        verifier: Final = secrets.token_urlsafe(48)
        challenge: Final = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not await self._state.create(state, cookie, nonce, verifier, target):
            return OidcFailure("unavailable")
        query: Final = urlencode({
            "client_id": self.config.client_id, "redirect_uri": self.config.callback_uri(),
            "response_type": "code", "response_mode": "query", "scope": "openid", "state": state,
            "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return OidcBegin(f"{snapshot.metadata.authorization_endpoint}?{query}", SecretStr(cookie))

    async def complete(
        self, *, state: str, code: str | None = None, cookie: str,
        issuer: str | None = None, error: str | None = None,
    ) -> OidcIdentity | OidcFailure:
        if (
            not _safe_parameter(state, 128) or not _safe_parameter(cookie, 128)
            or (code is None) == (error is None)
            or (code is not None and not _safe_parameter(code, 4096))
            or (error is not None and not _safe_parameter(error, 128))
        ):
            return OidcFailure("invalid")
        try:
            return await asyncio.wait_for(self._complete(state, code, cookie, issuer, error), timeout=12)
        except TimeoutError:
            return OidcFailure("unavailable")

    async def _complete(
        self, state: str, code: str | None, cookie: str, issuer: str | None, error: str | None,
    ) -> OidcIdentity | OidcFailure:
        async with self._callbacks:
            flow: Final = await self._state.consume(state, cookie)
            if isinstance(flow, OidcFailure):
                return flow
            snapshot: Final = await self._snapshot()
            if isinstance(snapshot, OidcFailure):
                return snapshot
            if (
                (issuer is not None and issuer != self.config.issuer)
                or (snapshot.metadata.authorization_response_iss_parameter_supported and issuer is None)
            ):
                return OidcFailure("invalid")
            if error is not None or code is None:
                return OidcFailure("denied")
            token: Final = await self._provider.exchange(snapshot, SecretStr(code), flow.verifier)
            if isinstance(token, OidcFailure):
                return token
            kid: Final = self._provider.token_kid(token.id_token)
            if isinstance(kid, OidcFailure):
                return kid
            checked: Final = (
                await self._rotated(snapshot, kid) if not any(key.kid == kid for key in snapshot.keys) else snapshot
            )
            if isinstance(checked, OidcFailure):
                return checked
            subject: Final = self._provider.verify(checked, token, flow.nonce_digest)
            if isinstance(subject, OidcFailure):
                return subject
            binding: Final = self.config.binding(subject.subject.get_secret_value())
            if binding is None:
                return OidcFailure("denied")
            if not await self._state.active():
                return OidcFailure("unavailable")
            return OidcIdentity(
                subject.subject, binding.user_id, flow.return_to, self.config.digest(),
                self.config.generation, self.config.namespace,
            )

    async def _rotated(self, previous: OidcSnapshot, kid: str) -> OidcSnapshot | OidcFailure:
        refreshed: Final = await self._refresh(force=True)
        if isinstance(refreshed, OidcSnapshot) and any(key.kid == kid for key in refreshed.keys):
            return refreshed
        for _ in range(10):
            await asyncio.sleep(0.05)
            shared: Final = await self._snapshot()
            if isinstance(shared, OidcSnapshot) and shared.fetched_at != previous.fetched_at:
                return shared
        return OidcFailure("invalid")

    async def close(self) -> None:
        task: Final = self._task
        self._task = None
        self._available = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._provider.close()

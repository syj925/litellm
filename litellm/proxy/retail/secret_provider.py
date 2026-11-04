import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from io import BytesIO
from typing import Final, Literal, Protocol, TypeAlias

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool, StrictInt, ValidationError

from .secret_config import ManagedSecretsConfig, SecretBinding

_PROVIDER_IO: Final = ContextVar("retail_secret_provider_io", default=False)
_HTTP_LOGGERS: Final = (
    "httpx", "httpcore.connection", "httpcore.connection_pool", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)


class _ProviderLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PROVIDER_IO.get()


@dataclass(frozen=True, slots=True)
class SecretValue:
    value: SecretStr
    version: int


@dataclass(frozen=True, slots=True)
class SecretFailure:
    code: Literal["unavailable", "revoked", "invalid"]


SecretResult: TypeAlias = SecretValue | SecretFailure


class SecretProvider(Protocol):
    async def read(self, binding: SecretBinding) -> SecretResult: ...

    async def close(self) -> None: ...


class _Metadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    version: StrictInt
    destroyed: StrictBool = False
    deletion_time: str = ""


class _VersionData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    data: Mapping[str, object] | None
    metadata: _Metadata


class _Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    data: _VersionData


class VaultKV2Provider:
    def __init__(
        self,
        config: ManagedSecretsConfig,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pause: Callable[[float], Awaitable[None]],
    ) -> None:
        self._config = config
        self._pause = pause
        self._log_filter = _ProviderLogFilter()
        self._client = httpx.AsyncClient(
            base_url=config.endpoint,
            headers={
                "X-Vault-Token": token.get_secret_value(),
                **({"X-Vault-Namespace": config.namespace} if config.namespace is not None else {}),
            },
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency),
            transport=transport,
        )
        for name in _HTTP_LOGGERS:
            logging.getLogger(name).addFilter(self._log_filter)

    async def read(self, binding: SecretBinding) -> SecretResult:
        if binding not in self._config.refs:
            return SecretFailure("invalid")
        for attempt in range(self._config.retries + 1):
            result: Final = await self._read_once(binding)
            if not isinstance(result, SecretFailure) or result.code != "unavailable":
                return result
            if attempt < self._config.retries:
                await self._pause(0.05 * (attempt + 1))
        return SecretFailure("unavailable")

    async def _read_once(self, binding: SecretBinding) -> SecretResult:
        token: Final = _PROVIDER_IO.set(True)
        try:
            return await asyncio.wait_for(self._request(binding), timeout=self._config.timeout_seconds)
        except (httpx.HTTPError, OSError, asyncio.TimeoutError):
            return SecretFailure("unavailable")
        except (ValidationError, ValueError):
            return SecretFailure("invalid")
        finally:
            _PROVIDER_IO.reset(token)

    async def _request(self, binding: SecretBinding) -> SecretResult:
        async with self._client.stream("GET", f"/v1/{self._config.mount}/data/{binding.path}") as response:
            if response.status_code in (401, 403, 404, 410):
                return SecretFailure("revoked")
            if response.status_code == 429 or response.status_code >= 500:
                return SecretFailure("unavailable")
            if response.status_code != 200:
                return SecretFailure("invalid")
            body: Final = BytesIO()
            async for part in response.aiter_bytes(chunk_size=4096):
                if body.tell() + len(part) > self._config.max_response_bytes:
                    return SecretFailure("invalid")
                body.write(part)
            payload: Final = _Envelope.model_validate_json(body.getvalue())
        metadata: Final = payload.data.metadata
        if metadata.destroyed or metadata.deletion_time or payload.data.data is None:
            return SecretFailure("revoked")
        value: Final = payload.data.data.get(binding.field)
        if (
            metadata.version < 1
            or not isinstance(value, str)
            or not value
            or len(value) > 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            return SecretFailure("invalid")
        return SecretValue(SecretStr(value), metadata.version)

    async def close(self) -> None:
        token: Final = _PROVIDER_IO.set(True)
        try:
            await self._client.aclose()
        finally:
            _PROVIDER_IO.reset(token)
            for name in _HTTP_LOGGERS:
                logging.getLogger(name).removeFilter(self._log_filter)

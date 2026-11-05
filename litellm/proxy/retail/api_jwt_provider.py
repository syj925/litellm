import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from typing import Final, Literal
from uuid import UUID

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator, model_validator

from .api_jwt_config import ApiJwtConfig, ApiJwtFailure
from .oidc_provider import OidcJwk

_JWT_IO: Final = ContextVar("retail_api_jwt_io", default=False)
_LOG_NAMES: Final = (
    "httpx", "httpcore.connection", "httpcore.connection_pool", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)
_PRIVATE_JWK_FIELDS: Final = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


def _public_keys(value: object) -> object:
    match value:
        case {"keys": list() as keys}:
            for key in keys:
                if isinstance(key, Mapping) and any(field in key for field in _PRIVATE_JWK_FIELDS):
                    raise ValueError("Only public API identity keys are accepted")
        case _:
            pass
    return value


@dataclass(frozen=True, slots=True)
class VerifiedApiJwt:
    subject: SecretStr = field(repr=False)
    client_id: SecretStr = field(repr=False)
    binding_revision: UUID
    issued_at: int
    expires_at: datetime


class ApiJwtSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    keys: tuple[OidcJwk, ...] = Field(min_length=1, max_length=16, repr=False)
    fetched_at: float = Field(allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def public_keys(cls, value: object) -> object:
        return _public_keys(value)

    @field_validator("keys")
    @classmethod
    def unique_keys(cls, value: tuple[OidcJwk, ...]) -> tuple[OidcJwk, ...]:
        if len(frozenset(key.kid for key in value)) != len(value):
            raise ValueError("Duplicate API identity key")
        return value


class _Jwks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    keys: tuple[OidcJwk, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def public_keys(cls, value: object) -> object:
        return _public_keys(value)


class _Header(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    alg: Literal["RS256"]
    typ: Literal["at+jwt", "application/at+jwt"]
    kid: str = Field(min_length=1, max_length=128, repr=False)


class _Claims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    iss: str = Field(repr=False)
    sub: str = Field(min_length=1, max_length=255, repr=False)
    aud: str | tuple[str, ...] = Field(repr=False)
    exp: StrictInt
    iat: StrictInt
    nbf: StrictInt | None = None
    client_id: str = Field(min_length=1, max_length=512, repr=False)
    jti: str = Field(min_length=1, max_length=128, repr=False)
    scope: str = Field(min_length=1, max_length=2048, repr=False)
    gateway_revision: UUID

    @field_validator("sub", "client_id", "jti", "scope")
    @classmethod
    def bounded_claim(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise ValueError("Invalid API identity claim")
        return value


class _ApiJwtLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _JWT_IO.get()


def _json_pairs(pairs: Sequence[tuple[str, object]]) -> Mapping[str, object]:
    if len(frozenset(key for key, _ in pairs)) != len(pairs):
        raise ValueError("Duplicate API identity field")
    return dict(pairs)


def _json(body: bytes) -> object:
    return json.loads(body, object_pairs_hook=_json_pairs)


@lru_cache(maxsize=64)
def _verification_key(source: OidcJwk) -> jwt.PyJWK:
    return jwt.PyJWK.from_dict(source.model_dump())


class ApiJwtProvider:
    def __init__(
        self, config: ApiJwtConfig, *, transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._filter = _ApiJwtLogFilter()
        self._client = httpx.AsyncClient(
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
            timeout=httpx.Timeout(3, connect=2, pool=0.5), follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2), transport=transport,
        )
        for name in _LOG_NAMES:
            logging.getLogger(name).addFilter(self._filter)

    async def fetch(self) -> ApiJwtSnapshot | ApiJwtFailure:
        context: Final = _JWT_IO.set(True)
        try:
            async with asyncio.timeout(5), self._client.stream("GET", self.config.jwks_uri) as response:
                if response.status_code != 200:
                    return ApiJwtFailure("unavailable")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return ApiJwtFailure("invalid")
                body: Final = BytesIO()
                async for part in response.aiter_bytes(chunk_size=4096):
                    if body.tell() + len(part) > 65536:
                        return ApiJwtFailure("invalid")
                    body.write(part)
                keys: Final = _Jwks.model_validate(_json(body.getvalue())).keys
                return ApiJwtSnapshot(keys=keys, fetched_at=self._clock())
        except (TimeoutError, httpx.HTTPError, OSError):
            return ApiJwtFailure("unavailable")
        except (ValueError, jwt.PyJWTError, RecursionError):
            return ApiJwtFailure("invalid")
        finally:
            _JWT_IO.reset(context)

    def token_kid(self, token: SecretStr) -> str | ApiJwtFailure:
        try:
            if not 1 <= len(token.get_secret_value()) <= 16384:
                return ApiJwtFailure("invalid")
            segments: Final = token.get_secret_value().split(".")
            if len(segments) != 3 or any(not segment for segment in segments):
                return ApiJwtFailure("invalid")
            if not isinstance(_json(jwt.utils.base64url_decode(segments[1])), Mapping):
                return ApiJwtFailure("invalid")
            return _Header.model_validate(_json(jwt.utils.base64url_decode(segments[0]))).kid
        except (ValueError, jwt.PyJWTError, RecursionError):
            return ApiJwtFailure("invalid")

    def verify(self, snapshot: ApiJwtSnapshot, token: SecretStr) -> VerifiedApiJwt | ApiJwtFailure:
        kid: Final = self.token_kid(token)
        if isinstance(kid, ApiJwtFailure):
            return kid
        source: Final = next((key for key in snapshot.keys if key.kid == kid), None)
        if source is None:
            return ApiJwtFailure("invalid")
        try:
            key: Final = _verification_key(source)
            claims: Final = _Claims.model_validate(jwt.decode(
                token.get_secret_value(), key=key.key, algorithms=["RS256"],
                audience=self.config.audience, issuer=self.config.issuer, leeway=0,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "client_id", "jti", "scope", "gateway_revision"]},
            ))
            expires: Final = datetime.fromtimestamp(claims.exp, timezone.utc)
        except (ValueError, jwt.PyJWTError, RecursionError, OverflowError, OSError):
            return ApiJwtFailure("invalid")
        now: Final = self._clock()
        if (
            claims.aud not in (self.config.audience, (self.config.audience,))
            or claims.client_id != self.config.client_id
            or claims.iat > now or claims.exp <= now
            or not 0 < claims.exp - claims.iat <= self.config.max_token_seconds
            or (claims.nbf is not None and claims.nbf > now)
            or self.config.required_scope not in claims.scope.split(" ")
        ):
            return ApiJwtFailure("invalid")
        return VerifiedApiJwt(
            SecretStr(claims.sub), SecretStr(claims.client_id), claims.gateway_revision, claims.iat, expires,
        )

    async def close(self) -> None:
        context: Final = _JWT_IO.set(True)
        try:
            await self._client.aclose()
        finally:
            _JWT_IO.reset(context)
            for name in _LOG_NAMES:
                logging.getLogger(name).removeFilter(self._filter)

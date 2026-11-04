import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from io import BytesIO
from typing import Final, Literal, Self
from urllib.parse import quote_plus

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, field_validator, model_validator

from .oidc_config import OidcConfig, OidcFailure, url_origin, valid_oidc_url

_OIDC_IO: Final = ContextVar("retail_oidc_io", default=False)
_LOG_NAMES: Final = (
    "httpx", "httpcore.connection", "httpcore.connection_pool", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)


class _OidcLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _OIDC_IO.get()


class OidcMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    issuer: str = Field(repr=False)
    authorization_endpoint: str = Field(repr=False)
    token_endpoint: str = Field(repr=False)
    jwks_uri: str = Field(repr=False)
    response_types_supported: tuple[str, ...]
    id_token_signing_alg_values_supported: tuple[str, ...]
    token_endpoint_auth_methods_supported: tuple[str, ...] = ("client_secret_basic",)
    code_challenge_methods_supported: tuple[str, ...] | None = None
    authorization_response_iss_parameter_supported: StrictBool = False


class OidcJwk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    kty: Literal["RSA"]
    kid: str = Field(min_length=1, max_length=128, repr=False)
    n: str = Field(min_length=32, max_length=1024, repr=False)
    e: str = Field(min_length=1, max_length=8, repr=False)
    alg: Literal["RS256"] = "RS256"
    use: Literal["sig"] = "sig"
    key_ops: tuple[Literal["verify"], ...] = ("verify",)

    @model_validator(mode="after")
    def valid_key(self) -> Self:
        if self.key_ops != ("verify",):
            raise ValueError("Invalid identity key")
        try:
            key: Final = jwt.PyJWK.from_dict(self.model_dump())
        except (ValueError, jwt.PyJWTError):
            raise ValueError("Invalid identity key") from None
        if not isinstance(key.key, RSAPublicKey) or not 2048 <= key.key.key_size <= 4096:
            raise ValueError("Invalid identity key")
        return self


class _Jwks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    keys: tuple[OidcJwk, ...] = Field(min_length=1, max_length=16)


class OidcSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    metadata: OidcMetadata = Field(repr=False)
    keys: tuple[OidcJwk, ...] = Field(min_length=1, max_length=16, repr=False)
    fetched_at: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len(frozenset(key.kid for key in self.keys)) != len(self.keys):
            raise ValueError("Duplicate identity key")
        return self


class _TokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    id_token: SecretStr = Field(repr=False)
    access_token: SecretStr | None = Field(default=None, repr=False)
    token_type: str = "Bearer"


class _Header(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    alg: Literal["RS256"]
    kid: str = Field(min_length=1, max_length=128, repr=False)
    typ: Literal["JWT"] | None = None


class _Claims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    iss: str = Field(repr=False)
    sub: str = Field(min_length=1, max_length=255, repr=False)
    aud: str | tuple[str, ...] = Field(repr=False)
    exp: StrictInt
    iat: StrictInt
    nbf: StrictInt | None = None
    nonce: str = Field(min_length=1, max_length=128, repr=False)
    azp: str | None = Field(default=None, repr=False)
    at_hash: str | None = Field(default=None, max_length=128, repr=False)

    @field_validator("sub")
    @classmethod
    def valid_subject(cls, value: str) -> str:
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("Invalid identity subject")
        return value


@dataclass(frozen=True, slots=True)
class OidcSubject:
    subject: SecretStr


def _json_pairs(pairs: Sequence[tuple[str, object]]) -> Mapping[str, object]:
    if len(frozenset(key for key, _ in pairs)) != len(pairs):
        raise ValueError("Duplicate identity field")
    return dict(pairs)


def _json(body: bytes) -> object:
    return json.loads(body, object_pairs_hook=_json_pairs)


class OidcProvider:
    def __init__(
        self,
        config: OidcConfig,
        client_secret: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._secret = client_secret
        self._clock = clock
        self._semaphore = asyncio.Semaphore(4)
        self._filter = _OidcLogFilter()
        self._client = httpx.AsyncClient(
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
            timeout=httpx.Timeout(3, connect=2, pool=0.5),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            transport=transport,
        )
        for name in _LOG_NAMES:
            logging.getLogger(name).addFilter(self._filter)

    def validate_metadata(self, metadata: OidcMetadata) -> bool:
        try:
            for endpoint in (metadata.authorization_endpoint, metadata.token_endpoint, metadata.jwks_uri):
                valid_oidc_url(endpoint, loopback=self._config.allow_loopback_http)
                if url_origin(endpoint) not in self._config.allowed_endpoint_origins():
                    return False
        except ValueError:
            return False
        return (
            metadata.issuer == self._config.issuer
            and "code" in metadata.response_types_supported
            and "RS256" in metadata.id_token_signing_alg_values_supported
            and self._config.client_auth_method in metadata.token_endpoint_auth_methods_supported
            and (
                metadata.code_challenge_methods_supported is None
                or "S256" in metadata.code_challenge_methods_supported
            )
        )

    def validate_snapshot(self, snapshot: OidcSnapshot) -> bool:
        return self.validate_metadata(snapshot.metadata)

    async def discover(self) -> OidcSnapshot | OidcFailure:
        try:
            return await asyncio.wait_for(self._discover(), timeout=5)
        except (TimeoutError, httpx.HTTPError, OSError):
            return OidcFailure("unavailable")
        except (ValueError, jwt.PyJWTError, RecursionError):
            return OidcFailure("invalid")

    async def _discover(self) -> OidcSnapshot | OidcFailure:
        body: Final = await self._request("GET", f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration")
        if isinstance(body, OidcFailure):
            return body
        metadata: Final = OidcMetadata.model_validate(_json(body))
        return await self._fetch_keys(metadata)

    async def _fetch_keys(self, metadata: OidcMetadata) -> OidcSnapshot | OidcFailure:
        if not self.validate_metadata(metadata):
            return OidcFailure("invalid")
        body: Final = await self._request("GET", metadata.jwks_uri)
        if isinstance(body, OidcFailure):
            return body
        keys: Final = _Jwks.model_validate(_json(body))
        return OidcSnapshot(metadata=metadata, keys=keys.keys, fetched_at=self._clock())

    async def _request(
        self, method: Literal["GET", "POST"], url: str, data: Mapping[str, str] | None = None,
        auth: httpx.BasicAuth | None = None,
    ) -> bytes | OidcFailure:
        context: Final = _OIDC_IO.set(True)
        try:
            async with self._semaphore, self._client.stream(method, url, data=data, auth=auth) as response:
                if response.status_code != 200:
                    return OidcFailure("unavailable" if response.status_code >= 500 else "invalid")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return OidcFailure("invalid")
                body: Final = BytesIO()
                async for part in response.aiter_bytes(chunk_size=4096):
                    if body.tell() + len(part) > 65536:
                        return OidcFailure("invalid")
                    body.write(part)
                return body.getvalue()
        finally:
            _OIDC_IO.reset(context)

    def token_kid(self, token: SecretStr) -> str | OidcFailure:
        try:
            if len(token.get_secret_value()) > 16384:
                return OidcFailure("invalid")
            segments: Final = token.get_secret_value().split(".")
            if len(segments) != 3:
                return OidcFailure("invalid")
            _json(jwt.utils.base64url_decode(segments[1]))
            return _Header.model_validate(_json(jwt.utils.base64url_decode(segments[0]))).kid
        except (ValueError, jwt.PyJWTError, RecursionError):
            return OidcFailure("invalid")

    async def exchange(
        self, snapshot: OidcSnapshot, code: SecretStr, verifier: SecretStr,
    ) -> _TokenResponse | OidcFailure:
        if not self.validate_snapshot(snapshot):
            return OidcFailure("invalid")
        data: Final = {
            "grant_type": "authorization_code", "code": code.get_secret_value(),
            "redirect_uri": self._config.callback_uri(), "code_verifier": verifier.get_secret_value(),
            **({"client_id": self._config.client_id, "client_secret": self._secret.get_secret_value()}
               if self._config.client_auth_method == "client_secret_post" else {}),
        }
        auth: Final = (
            httpx.BasicAuth(
                quote_plus(self._config.client_id, safe=""), quote_plus(self._secret.get_secret_value(), safe=""),
            )
            if self._config.client_auth_method == "client_secret_basic" else None
        )
        try:
            body: Final = await asyncio.wait_for(
                self._request("POST", snapshot.metadata.token_endpoint, data, auth), timeout=5,
            )
            if isinstance(body, OidcFailure):
                return body
            response: Final = _TokenResponse.model_validate(_json(body))
        except (TimeoutError, httpx.HTTPError, OSError):
            return OidcFailure("unavailable")
        except (ValueError, RecursionError):
            return OidcFailure("invalid")
        else:
            return response if response.token_type.lower() == "bearer" else OidcFailure("invalid")

    def verify(self, snapshot: OidcSnapshot, token: _TokenResponse, nonce_digest: str) -> OidcSubject | OidcFailure:
        kid: Final = self.token_kid(token.id_token)
        if isinstance(kid, OidcFailure):
            return kid
        source: Final = next((key for key in snapshot.keys if key.kid == kid), None)
        if source is None or not self.validate_snapshot(snapshot):
            return OidcFailure("invalid")
        try:
            key: Final = jwt.PyJWK.from_dict(source.model_dump())
            claims: Final = _Claims.model_validate(jwt.decode(
                token.id_token.get_secret_value(), key=key.key, algorithms=["RS256"],
                audience=self._config.client_id, issuer=self._config.issuer, leeway=30,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            ))
        except (ValueError, jwt.PyJWTError, RecursionError):
            return OidcFailure("invalid")
        if (
            claims.aud not in (self._config.client_id, (self._config.client_id,))
            or claims.azp not in (None, self._config.client_id)
            or claims.iat < self._clock() - 330
            or not hmac.compare_digest(hashlib.sha256(claims.nonce.encode()).hexdigest(), nonce_digest)
        ):
            return OidcFailure("invalid")
        if claims.at_hash is not None:
            if token.access_token is None:
                return OidcFailure("invalid")
            digest: Final = hashlib.sha256(token.access_token.get_secret_value().encode()).digest()[:16]
            encoded: Final = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if not hmac.compare_digest(encoded, claims.at_hash):
                return OidcFailure("invalid")
        return OidcSubject(SecretStr(claims.sub))

    async def close(self) -> None:
        context: Final = _OIDC_IO.set(True)
        try:
            await self._client.aclose()
        finally:
            _OIDC_IO.reset(context)
            for name in _LOG_NAMES:
                logging.getLogger(name).removeFilter(self._filter)

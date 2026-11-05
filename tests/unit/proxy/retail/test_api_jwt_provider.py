import json
import logging
import time
from collections.abc import Mapping
from typing import Final
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from litellm.proxy.retail.api_jwt_config import ApiJwtConfig, ApiJwtFailure
from litellm.proxy.retail.api_jwt_provider import ApiJwtProvider, ApiJwtSnapshot, VerifiedApiJwt

REVISION: Final = UUID("54fc0355-6133-45e2-81a7-1f60bf78fc5e")


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def config() -> ApiJwtConfig:
    return ApiJwtConfig.model_validate({
        "namespace": "unit-identity", "generation": 1, "issuer": "https://issuer.test", "audience": "gateway",
        "client_id": "private-client", "jwks_uri": "https://keys.test/jwks",
        "bindings": ({"subject": "private-subject", "user_id": "owner", "token_hash": "a" * 64,
                      "binding_revision": str(REVISION)},),
    })


def public_key(key: rsa.RSAPrivateKey) -> Mapping[str, object]:
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "fixture"}


def snapshot(key: rsa.RSAPrivateKey) -> ApiJwtSnapshot:
    return ApiJwtSnapshot.model_validate({"keys": [public_key(key)], "fetched_at": time.time()})


def claims() -> Mapping[str, object]:
    now: Final = int(time.time())
    return {
        "iss": config().issuer, "sub": "private-subject", "aud": config().audience,
        "exp": now + 120, "iat": now, "client_id": "private-client", "jti": "private-id",
        "scope": "models:invoke", "gateway_revision": str(REVISION),
        "role": "proxy_admin", "email": "ignored@example.invalid",
    }


def token(key: rsa.RSAPrivateKey, overrides: Mapping[str, object] | None = None) -> SecretStr:
    return SecretStr(jwt.encode({**claims(), **(overrides or {})}, key, algorithm="RS256", headers={
        "kid": "fixture", "typ": "at+jwt",
    }))


@pytest.mark.asyncio
async def test_verified_identity_excludes_raw_tokens_extra_claims_and_jti(key: rsa.RSAPrivateKey) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        identity: Final = provider.verify(snapshot(key), token(key))
        assert isinstance(identity, VerifiedApiJwt)
        assert identity.subject.get_secret_value() == "private-subject"
        assert identity.client_id.get_secret_value() == "private-client"
        assert identity.binding_revision == REVISION and identity.expires_at.tzinfo is not None
        assert "private-" not in repr(identity) and "proxy_admin" not in repr(identity)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", (
    ("iss", "https://other.test"), ("aud", "other"), ("aud", ("gateway", "other")),
    ("client_id", "other"), ("scope", "models:invoke-other"), ("scope", "other"),
    ("exp", True), ("iat", True), ("jti", ""), ("gateway_revision", "not-a-uuid"),
))
async def test_signed_but_wrong_purpose_or_claims_fail(
    key: rsa.RSAPrivateKey, field: str, value: object,
) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        assert provider.verify(snapshot(key), token(key, {field: value})) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("future-issued", "expired", "not-yet-valid", "long-lived"))
async def test_each_time_rule_is_enforced_for_otherwise_valid_signed_tokens(key: rsa.RSAPrivateKey, kind: str) -> None:
    now: Final = int(time.time())
    changes: Final = {
        "future-issued": {"iat": now + 60, "exp": now + 180},
        "expired": {"iat": now - 180, "exp": now - 60},
        "not-yet-valid": {"nbf": now + 60},
        "long-lived": {"exp": now + config().max_token_seconds + 60},
    }
    provider: Final = ApiJwtProvider(config())
    try:
        assert provider.verify(snapshot(key), token(key, changes[kind])) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_access_token_required_fields_are_not_optional(key: rsa.RSAPrivateKey) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        for name in ("iss", "sub", "aud", "exp", "iat", "client_id", "jti", "scope", "gateway_revision"):
            stripped: Final = {field: value for field, value in claims().items() if field != name}
            encoded: Final = SecretStr(jwt.encode(stripped, key, algorithm="RS256", headers={
                "kid": "fixture", "typ": "at+jwt",
            }))
            assert provider.verify(snapshot(key), encoded) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_http_log_filter_is_scoped_to_owned_io_and_does_not_silence_other_tasks(
    key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture,
) -> None:
    def dispatch(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpx").warning("private-jwks-transport-detail")
        return httpx.Response(200, json={"keys": [public_key(key)]})

    provider: Final = ApiJwtProvider(config(), transport=httpx.MockTransport(dispatch))
    try:
        with caplog.at_level(logging.WARNING, logger="httpx"):
            assert isinstance(await provider.fetch(), ApiJwtSnapshot)
            logging.getLogger("httpx").warning("unrelated-request-still-visible")
        assert "private-jwks-transport-detail" not in caplog.text
        assert "unrelated-request-still-visible" in caplog.text
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ({"typ": "JWT"}, {"alg": "HS256"}, {"jku": "https://untrusted.test"},
                                  {"crit": ["extension"]}, {"b64": False}, {"kid": "unknown"}))
async def test_jose_extensions_id_tokens_and_wrong_key_cannot_be_substituted(
    key: rsa.RSAPrivateKey, extra: Mapping[str, object],
) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        headers: Final = {"alg": "RS256", "kid": "fixture", "typ": "at+jwt", **extra}
        header: Final = jwt.utils.base64url_encode(json.dumps(headers).encode())
        payload: Final = jwt.utils.base64url_encode(json.dumps(claims()).encode())
        signing: Final = header + b"." + payload
        signature: Final = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        encoded: Final = SecretStr((signing + b"." + jwt.utils.base64url_encode(signature)).decode())
        assert provider.verify(snapshot(key), encoded) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_duplicate_json_fields_are_rejected_even_when_signed(key: rsa.RSAPrivateKey) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        normal_header: Final = '{"alg":"RS256","typ":"at+jwt","kid":"fixture"}'
        normal_payload: Final = json.dumps(claims())
        for header, payload in (
            (normal_header[:-1] + ',"alg":"RS256"}', normal_payload),
            (normal_header, normal_payload[:-1] + ',"aud":"gateway"}'),
        ):
            signing: Final = jwt.utils.base64url_encode(header.encode()) + b"." + jwt.utils.base64url_encode(payload.encode())
            signature: Final = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
            encoded: Final = SecretStr((signing + b"." + jwt.utils.base64url_encode(signature)).decode())
            assert provider.verify(snapshot(key), encoded) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_bad_signature_and_resource_bounds(key: rsa.RSAPrivateKey) -> None:
    provider: Final = ApiJwtProvider(config())
    try:
        other: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert provider.verify(snapshot(key), token(other)) == ApiJwtFailure("invalid")
        assert provider.token_kid(SecretStr("x" * 16385)) == ApiJwtFailure("invalid")
        assert provider.token_kid(SecretStr("not.jwt")) == ApiJwtFailure("invalid")
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", (
    httpx.Response(307, headers={"location": "https://untrusted.test"}),
    httpx.Response(200, content=b"x" * 65537),
    httpx.Response(200, headers={"Content-Encoding": "br"}, content=b"invalid"),
    httpx.Response(503),
))
async def test_jwks_http_failures_are_bounded_and_not_redirected(response: httpx.Response) -> None:
    calls: Final = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return response

    provider: Final = ApiJwtProvider(config(), transport=httpx.MockTransport(dispatch))
    try:
        assert isinstance(await provider.fetch(), ApiJwtFailure)
        assert len(calls) == 1 and str(calls[0]) == config().jwks_uri
    finally:
        await provider.close()

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Final
from urllib.parse import parse_qs, quote_plus

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from pydantic import SecretStr

from litellm.proxy.retail.oidc_config import OidcBinding, OidcConfig, OidcFailure
from litellm.proxy.retail.oidc_provider import OidcJwk, OidcMetadata, OidcProvider, OidcSnapshot, OidcSubject


def config(changes: Mapping[str, object] | None = None) -> OidcConfig:
    return OidcConfig.model_validate({
        "namespace": "qa", "generation": 1, "issuer": "https://idp.test", "client_id": "client:+%",
        "client_auth_method": "client_secret_post", "public_origin": "https://gateway.test",
        "bindings": (OidcBinding(subject="subject", user_id="user"),), **(changes or {}),
    })


@pytest.fixture(scope="module")
def signing_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


def metadata(changes: Mapping[str, object] | None = None) -> OidcMetadata:
    return OidcMetadata.model_validate({
        "issuer": "https://idp.test", "authorization_endpoint": "https://idp.test/auth",
        "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks",
        "response_types_supported": ["code"], "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"], **(changes or {}),
    })


def snapshot(signing_key: RSAPrivateKey) -> OidcSnapshot:
    jwk: Final = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    return OidcSnapshot(
        metadata=metadata(), keys=(OidcJwk.model_validate({**jwk, "kid": "one"}),), fetched_at=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", (
    {"iss": "https://foreign.test"}, {"aud": ["client:+%", "untrusted"]}, {"azp": "foreign"},
    {"nonce": "wrong"}, {"iat": True}, {"iat": -1000}, {"exp": 0}, {"nbf": 9999999999},
    {"sub": "bad\nsubject"}, {"nonce": None},
))
async def test_signed_but_invalid_identity_never_accepted(
    signing_key: RSAPrivateKey, changes: Mapping[str, object],
) -> None:
    claims: Final = {
        "iss": "https://idp.test", "sub": "subject", "aud": "client:+%", "iat": 0,
        "exp": 9999999999, "nonce": "nonce", **changes,
    }
    encoded: Final = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "one"})
    provider: Final = OidcProvider(
        config(), SecretStr("private-client-secret"), clock=lambda: 0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id_token": encoded})),
    )
    token: Final = await provider.exchange(snapshot(signing_key), SecretStr("private-code"), SecretStr("verifier"))
    assert not isinstance(token, OidcFailure)
    result: Final = provider.verify(snapshot(signing_key), token, hashlib.sha256(b"nonce").hexdigest())
    assert result == OidcFailure("invalid")
    assert "private" not in repr(result)
    await provider.close()


@pytest.mark.asyncio
async def test_signature_and_nonce_verified_and_claim_role_is_unused(signing_key: RSAPrivateKey) -> None:
    encoded: Final = jwt.encode({
        "iss": "https://idp.test", "sub": "subject", "aud": ["client:+%"], "iat": 0,
        "exp": 9999999999, "nonce": "nonce", "role": "proxy_admin", "email": "admin@test",
    }, signing_key, algorithm="RS256", headers={"kid": "one"})

    def respond(request: httpx.Request) -> httpx.Response:
        form: Final = parse_qs(request.content.decode())
        assert form["client_id"] == [config().client_id]
        assert form["client_secret"] == ["private-client-secret"]
        assert form["redirect_uri"] == [config().callback_uri()]
        return httpx.Response(200, json={"id_token": encoded})

    provider: Final = OidcProvider(
        config(), SecretStr("private-client-secret"), clock=lambda: 0, transport=httpx.MockTransport(respond),
    )
    token: Final = await provider.exchange(snapshot(signing_key), SecretStr("private-code"), SecretStr("verifier"))
    assert not isinstance(token, OidcFailure)
    assert provider.verify(snapshot(signing_key), token, hashlib.sha256(b"nonce").hexdigest()) == OidcSubject(
        SecretStr("subject"),
    )
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", (
    {"issuer": "https://different.test"}, {"jwks_uri": "https://foreign.test/jwks"},
    {"token_endpoint": "http://idp.test/token"}, {"code_challenge_methods_supported": ["plain"]},
))
async def test_discovery_cannot_change_identity_or_network_boundary(changes: Mapping[str, object]) -> None:
    provider: Final = OidcProvider(config(), SecretStr("private-client-secret"))
    assert not provider.validate_metadata(metadata(changes))
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ("HS256", "none", "RS256"))
async def test_wrong_signature_and_algorithm_never_authenticate(algorithm: str, signing_key: RSAPrivateKey) -> None:
    claims: Final = {
        "iss": "https://idp.test", "sub": "subject", "aud": "client:+%", "iat": 0,
        "exp": 9999999999, "nonce": "nonce",
    }
    symmetric_key: Final = "" if algorithm == "none" else "fixture-hmac-key-with-sufficient-length"
    key: Final = generate_private_key(public_exponent=65537, key_size=2048) if algorithm == "RS256" else symmetric_key
    encoded: Final = jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "one"})
    provider: Final = OidcProvider(
        config(), SecretStr("private-client-secret"), clock=lambda: 0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id_token": encoded})),
    )
    token: Final = await provider.exchange(snapshot(signing_key), SecretStr("private-code"), SecretStr("verifier"))
    assert not isinstance(token, OidcFailure)
    assert provider.verify(snapshot(signing_key), token, hashlib.sha256(b"nonce").hexdigest()) == OidcFailure("invalid")
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("basic", (False, True))
async def test_oidc_log_filter_is_private_and_preserves_other_task_logging(
    caplog: pytest.LogCaptureFixture, signing_key: RSAPrivateKey, basic: bool,
) -> None:
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore.http11")
    settings: Final = config({"client_auth_method": "client_secret_basic" if basic else "client_secret_post"})
    secret: Final = "private-client:+%"

    def respond(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("%s %s", request.headers, request.content)
        return httpx.Response(503, text="private-provider-error")

    provider: Final = OidcProvider(
        settings, SecretStr(secret), transport=httpx.MockTransport(respond),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as normal:
        await asyncio.gather(
            provider.exchange(snapshot(signing_key), SecretStr("private-code"), SecretStr("private-verifier")),
            normal.get("https://public.test/visible"),
        )
    encoded_pair: Final = f"{quote_plus(settings.client_id, safe='')}:{quote_plus(secret, safe='')}"
    basic_marker: Final = base64.b64encode(encoded_pair.encode()).decode()
    assert "public.test/visible" in caplog.text
    assert "idp.test" not in caplog.text
    assert secret not in caplog.text
    assert quote_plus(secret, safe="") not in caplog.text
    assert basic_marker not in caplog.text
    assert "private-code" not in caplog.text
    assert "private-verifier" not in caplog.text
    assert "private-provider-error" not in caplog.text
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", (False, True))
async def test_only_configured_cross_origin_endpoints_are_fetched(allow: bool, signing_key: RSAPrivateKey) -> None:
    settings: Final = config({"endpoint_origins": ("https://keys.test",)} if allow else {})
    document: Final = {
        **metadata().model_dump(exclude={"code_challenge_methods_supported"}), "jwks_uri": "https://keys.test/jwks",
    }
    visited: Final = Requests()

    def respond(request: httpx.Request) -> httpx.Response:
        visited.record(str(request.url))
        if request.url.host == "idp.test":
            return httpx.Response(200, json=document)
        return httpx.Response(200, json={"keys": [snapshot(signing_key).keys[0].model_dump()]})

    provider: Final = OidcProvider(settings, SecretStr("private-client-secret"), transport=httpx.MockTransport(respond))
    result: Final = await provider.discover()
    assert isinstance(result, OidcSnapshot) if allow else result == OidcFailure("invalid")
    assert ("https://keys.test/jwks" in visited.values) is allow
    await provider.close()


class Requests:
    def __init__(self) -> None:
        self.values: tuple[str, ...] = ()

    def record(self, value: str) -> None:
        self.values = (*self.values, value)


@pytest.mark.asyncio
async def test_discovery_removes_terminal_slash_only_for_fetch_not_identity(signing_key: RSAPrivateKey) -> None:
    settings: Final = config({"issuer": "https://idp.test/tenant/"})
    visits: Final = Requests()

    def respond(request: httpx.Request) -> httpx.Response:
        visits.record(str(request.url))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=metadata({"issuer": settings.issuer}).model_dump())
        return httpx.Response(200, json={"keys": [snapshot(signing_key).keys[0].model_dump()]})

    provider: Final = OidcProvider(settings, SecretStr("private-client-secret"), transport=httpx.MockTransport(respond))
    result: Final = await provider.discover()
    assert isinstance(result, OidcSnapshot)
    assert result.metadata.issuer == settings.issuer
    assert visits.values[0] == "https://idp.test/tenant/.well-known/openid-configuration"
    assert not provider.validate_metadata(metadata({"issuer": "https://idp.test/tenant"}))
    await provider.close()

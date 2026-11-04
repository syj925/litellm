import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import parse_qs, unquote_plus, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from pydantic import SecretStr
from redis.asyncio import Redis

from litellm.proxy.retail.oidc_config import OidcBinding, OidcConfig, OidcFailure
from litellm.proxy.retail.oidc_runtime import OidcBegin, OidcIdentity, OidcRuntime
from litellm.proxy.retail.oidc_state import OidcState


@dataclass(frozen=True, slots=True)
class IssuedCode:
    nonce: str
    challenge: str


class Idp:
    def __init__(self) -> None:
        self._keys = tuple(generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
        self._codes: Mapping[str, IssuedCode] = {}
        self._lock = threading.Lock()
        self.port = 0
        self.key_number = 0
        self.discoveries = 0
        self.jwks_reads = 0
        self.exchanges = 0
        self.override: Mapping[str, object] = {}
        self.redirect = False
        self.oversized = False
        self.advertise_pkce = True
        self.token_delay = 0.0

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def rotate(self, number: int) -> None:
        self.key_number = number

    def claims(self, overrides: Mapping[str, object]) -> None:
        self.override = overrides

    def fail(self, oversized: bool) -> None:
        self.oversized = oversized
        self.redirect = not oversized

    def omit_pkce_advertisement(self) -> None:
        self.advertise_pkce = False

    def delay_tokens(self, seconds: float) -> None:
        self.token_delay = seconds

    def issue(self, params: Mapping[str, Sequence[str]]) -> str:
        code: Final = uuid.uuid4().hex
        with self._lock:
            self._codes = {**self._codes, code: IssuedCode(params["nonce"][0], params["code_challenge"][0])}
        return code

    def exchange(self, params: Mapping[str, Sequence[str]]) -> Mapping[str, object] | None:
        with self._lock:
            self.exchanges += 1
            code: Final = params.get("code", [""])[0]
            issued: Final = self._codes.get(code)
            self._codes = {name: value for name, value in self._codes.items() if name != code}
        if issued is None:
            return None
        challenge: Final = base64.urlsafe_b64encode(
            hashlib.sha256(params.get("code_verifier", [""])[0].encode()).digest(),
        ).rstrip(b"=").decode()
        if challenge != issued.challenge or params.get("redirect_uri") != [f"{self.origin}/retail/auth/oidc/callback"]:
            return None
        token: Final = jwt.encode({
            "iss": self.origin, "sub": "private-subject", "aud": "fixture:+%",
            "exp": int(time.time()) + 120, "iat": int(time.time()), "nonce": issued.nonce,
            **self.override,
        }, self._keys[self.key_number], algorithm="RS256", headers={"kid": str(self.key_number)})
        return {"id_token": token, "access_token": "private-unused-access-token", "token_type": "Bearer"}

    def jwks(self) -> Mapping[str, object]:
        self.jwks_reads += 1
        key: Final = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._keys[self.key_number].public_key()))
        return {"keys": [{**key, "kid": str(self.key_number)}]}

    def metadata(self) -> Mapping[str, object]:
        self.discoveries += 1
        return {
            "issuer": self.origin, "authorization_endpoint": f"{self.origin}/authorize",
            "token_endpoint": f"{self.origin}/token", "jwks_uri": f"{self.origin}/jwks",
            "response_types_supported": ["code"], "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            **({"code_challenge_methods_supported": ["S256"]} if self.advertise_pkce else {}),
            "authorization_response_iss_parameter_supported": True,
        }


@pytest.fixture
def idp() -> Iterator[Idp]:
    service: Final = Idp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def reply(self, status: int, body: Mapping[str, object]) -> None:
            data: Final = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed: Final = urlsplit(self.path)
            if service.redirect:
                self.send_response(307)
                self.send_header("Location", f"{service.origin}/private-redirect")
                self.end_headers()
                return
            if service.oversized:
                self.reply(200, {"private": "x" * 65537})
                return
            if parsed.path == "/.well-known/openid-configuration":
                self.reply(200, service.metadata())
                return
            if parsed.path == "/jwks":
                self.reply(200, service.jwks())
                return
            self.reply(404, {"error": "not_found"})

        def do_POST(self) -> None:
            params: Final = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            auth: Final = self.headers.get("Authorization", "")
            basic: Final = (
                tuple(unquote_plus(part) for part in base64.b64decode(auth[6:]).decode().split(":"))
                if auth.startswith("Basic ") else ()
            )
            authenticated: Final = basic == ("fixture:+%", "private-client:+%") or (
                params.get("client_id") == ["fixture:+%"] and params.get("client_secret") == ["private-client:+%"]
            )
            if not authenticated:
                self.reply(401, {"error": "invalid_client"})
                return
            result: Final = service.exchange(params)
            time.sleep(service.token_delay)
            try:
                self.reply(400 if result is None else 200, result or {"error": "invalid_grant"})
            except BrokenPipeError:
                return

    server: Final = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.port = server.server_port
    thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def config(idp: Idp, *, generation: int = 1, namespace: str | None = None, post: bool = False) -> OidcConfig:
    return OidcConfig(
        namespace=namespace or uuid.uuid4().hex, generation=generation, issuer=idp.origin, client_id="fixture:+%",
        client_auth_method="client_secret_post" if post else "client_secret_basic",
        public_origin=idp.origin, bindings=(OidcBinding(subject="private-subject", user_id="local-user"),),
        allow_loopback_http=True,
    )


def redis() -> Redis:
    return Redis.from_url(os.environ["RETAIL_OIDC_TEST_REDIS_URL"], max_connections=8, socket_timeout=1)


def authorize(service: Idp, flow: OidcBegin) -> tuple[str, str]:
    params: Final = parse_qs(urlsplit(flow.authorization_url).query)
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params
    return service.issue(params), params["state"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("post", (False, True))
async def test_browser_binding_cross_worker_single_consumption_and_private_identity(idp: Idp, post: bool) -> None:
    client: Final = redis()
    settings: Final = config(idp, post=post)
    first: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    second: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    try:
        await first.start()
        await second.start()
        flow: Final = await first.begin("/ui/logs/?log_id=one")
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        wrong: Final = await second.complete(state=state, cookie="unrelated-browser", code=code, issuer=idp.origin)
        assert wrong == OidcFailure("invalid")
        assert idp.exchanges == 0
        results: Final = await asyncio.gather(*(
            runtime.complete(state=state, cookie=flow.flow_cookie.get_secret_value(), code=code, issuer=idp.origin)
            for runtime in (first, second)
        ))
        identities: Final = tuple(result for result in results if isinstance(result, OidcIdentity))
        assert len(identities) == 1
        assert identities[0].user_id == "local-user"
        assert identities[0].return_to == "/ui/logs/?log_id=one"
        assert idp.exchanges == 1
        assert all("private-" not in repr(result) for result in results)
        assert idp.discoveries == idp.jwks_reads == 1
    finally:
        await first.close()
        await second.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_kid_refresh_is_shared_bounded_and_new_key_reaches_both_workers(idp: Idp) -> None:
    client: Final = redis()
    settings: Final = config(idp)
    first: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    second: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    try:
        await first.start()
        await second.start()
        idp.rotate(1)
        await client.delete(f"retail:oidc:{settings.namespace}:{settings.digest()}:cooldown")
        flows: Final = await asyncio.gather(*(first.begin() for _ in range(8)))
        assert all(isinstance(flow, OidcBegin) for flow in flows)

        async def complete_flow(flow: OidcBegin | OidcFailure, runtime: OidcRuntime) -> OidcIdentity | OidcFailure:
            assert isinstance(flow, OidcBegin)
            code, state = authorize(idp, flow)
            return await runtime.complete(
                state=state, cookie=flow.flow_cookie.get_secret_value(), code=code, issuer=idp.origin,
            )

        results: Final = await asyncio.gather(*(
            complete_flow(flow, first if index % 2 else second) for index, flow in enumerate(flows)
        ))
        assert all(isinstance(result, OidcIdentity) for result in results)
        assert idp.jwks_reads == 2
        idp.rotate(0)
        rejected: Final = await asyncio.gather(*(complete_flow(flow, first) for flow in (
            await first.begin(), await second.begin(),
        )))
        assert all(result == OidcFailure("invalid") for result in rejected)
        assert idp.jwks_reads == 2
    finally:
        await first.close()
        await second.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_generation_fences_stale_worker_flow_and_lease_publication(idp: Idp) -> None:
    client: Final = redis()
    settings: Final = config(idp)
    first: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    try:
        await first.start()
        flow: Final = await first.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        old: Final = OidcState(settings, client)
        snapshot: Final = await old.snapshot()
        assert not isinstance(snapshot, OidcFailure)
        await client.delete(f"retail:oidc:{settings.namespace}:{settings.digest()}:cooldown")
        owner: Final = await old.lease()
        assert isinstance(owner, str)
        newer: Final = OidcState(config(idp, namespace=settings.namespace, generation=2), client)
        assert await newer.activate()
        assert not await old.activate()
        assert not await old.publish(owner, snapshot)
        assert await first.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("invalid")
        assert isinstance(await first.begin(), OidcFailure)
        assert idp.exchanges == 0
    finally:
        await first.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", (
    {"nonce": "wrong"}, {"sub": "unknown", "email": "local-user", "role": "proxy_admin"},
    {"aud": "other"}, {"iss": "https://wrong.test"}, {"azp": "other"},
))
async def test_bad_or_unmapped_signed_identity_consumes_flow_without_token_leak(
    idp: Idp, overrides: Mapping[str, object],
) -> None:
    client: Final = redis()
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    try:
        await runtime.start()
        idp.claims(overrides)
        flow: Final = await runtime.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        result: Final = await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        )
        assert isinstance(result, OidcFailure)
        assert "private-" not in repr(result)
        assert await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("invalid")
        assert idp.exchanges == 1
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", (False, True))
async def test_discovery_redirect_or_oversize_does_not_enable_login(idp: Idp, oversized: bool) -> None:
    client: Final = redis()
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    idp.fail(oversized)
    try:
        await runtime.start()
        assert not runtime.status().available
        assert await runtime.begin() == OidcFailure("unavailable")
        assert idp.exchanges == idp.jwks_reads == 0
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_hard_expiry_of_flow_snapshot_and_lost_lease_fail_closed(idp: Idp) -> None:
    client: Final = redis()
    settings: Final = config(idp)
    runtime: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    store: Final = OidcState(settings, client)
    prefix: Final = f"retail:oidc:{settings.namespace}:{settings.digest()}"
    try:
        await runtime.start()
        flow: Final = await runtime.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        flow_key: Final = f"{prefix}:flow:{hashlib.sha256(state.encode()).hexdigest()}"
        assert 0 < await client.ttl(flow_key) <= 300
        assert 0 < await client.ttl(f"{prefix}:snapshot") <= 300
        await client.expire(flow_key, 0)
        assert await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("invalid")
        assert idp.exchanges == 0
        snapshot: Final = await store.snapshot()
        assert not isinstance(snapshot, OidcFailure)
        await client.delete(f"{prefix}:cooldown")
        owner: Final = await store.lease()
        assert isinstance(owner, str)
        await client.expire(f"{prefix}:lease", 0)
        assert not await store.publish(owner, snapshot)
        await client.expire(f"{prefix}:snapshot", 0)
        assert await runtime.begin() == OidcFailure("unavailable")
        assert not runtime.status().available
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_issuer_and_provider_error_never_exchange_and_cannot_replay(idp: Idp) -> None:
    client: Final = redis()
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    try:
        await runtime.start()
        flow: Final = await runtime.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        assert await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(),
        ) == OidcFailure("invalid")
        assert await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("invalid")
        denied: Final = await runtime.begin()
        assert isinstance(denied, OidcBegin)
        _, denied_state = authorize(idp, denied)
        assert await runtime.complete(
            state=denied_state, error="access_denied", cookie=denied.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("denied")
        assert idp.exchanges == 0
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_coordination_unavailable_does_not_fall_back_to_local_state(idp: Idp) -> None:
    client: Final = Redis.from_url("redis://127.0.0.1:1/0", max_connections=2, socket_connect_timeout=0.1)
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    try:
        await runtime.start()
        assert not runtime.status().available
        assert await runtime.begin() == OidcFailure("unavailable")
        assert await runtime.complete(state="state", code="code", cookie="cookie") == OidcFailure("unavailable")
        assert idp.discoveries == idp.jwks_reads == idp.exchanges == 0
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_absent_optional_pkce_metadata_still_uses_s256_and_verifier(idp: Idp) -> None:
    idp.omit_pkce_advertisement()
    client: Final = redis()
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    try:
        await runtime.start()
        flow: Final = await runtime.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        result: Final = await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        )
        assert isinstance(result, OidcIdentity)
        assert result.user_id == "local-user"
        assert idp.exchanges == 1
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_ambiguous_exchange_timeout_is_not_retried_or_restored(idp: Idp) -> None:
    client: Final = redis()
    runtime: Final = OidcRuntime(config(idp), client, SecretStr("private-client:+%"))
    try:
        await runtime.start()
        flow: Final = await runtime.begin()
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        idp.delay_tokens(6)
        began: Final = time.monotonic()
        result: Final = await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        )
        assert result == OidcFailure("unavailable")
        assert time.monotonic() - began < 6
        assert await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        ) == OidcFailure("invalid")
        assert idp.exchanges == 1
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_background_recovers_owned_coordination_loss_without_process_restart(idp: Idp) -> None:
    client: Final = redis()
    settings: Final = config(idp)
    runtime: Final = OidcRuntime(settings, client, SecretStr("private-client:+%"))
    prefix: Final = f"retail:oidc:{settings.namespace}"
    try:
        await runtime.start()
        assert runtime.status().available
        await client.delete(
            f"{prefix}:active", f"{prefix}:{settings.digest()}:snapshot", f"{prefix}:{settings.digest()}:cooldown",
        )
        assert await runtime.begin() == OidcFailure("unavailable")

        async def recovered() -> OidcBegin | OidcFailure:
            while not runtime.status().available:
                await asyncio.sleep(0.05)
            return await runtime.begin()

        flow: Final = await asyncio.wait_for(recovered(), timeout=8)
        assert isinstance(flow, OidcBegin)
        code, state = authorize(idp, flow)
        result: Final = await runtime.complete(
            state=state, code=code, cookie=flow.flow_cookie.get_secret_value(), issuer=idp.origin,
        )
        assert isinstance(result, OidcIdentity)
        assert result.user_id == "local-user"
        assert idp.jwks_reads == 2
    finally:
        await runtime.close()
        await client.aclose()

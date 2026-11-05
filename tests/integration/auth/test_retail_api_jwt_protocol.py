import asyncio
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from pydantic import SecretStr
from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from litellm.proxy.retail.api_jwt_config import ApiJwtConfig, ApiJwtFailure
from litellm.proxy.retail.api_jwt_provider import VerifiedApiJwt
from litellm.proxy.retail.api_jwt_runtime import ApiJwtRuntime


class CountingRedis(Redis):
    def __init__(self) -> None:
        super().__init__(connection_pool=ConnectionPool.from_url(
            os.environ["RETAIL_API_JWT_TEST_REDIS_URL"], max_connections=8, socket_timeout=1,
        ))
        self.calls = 0
        self.available = True

    def set_available(self, available: bool) -> None:
        self.available = available

    async def execute_command(
        self, *args: object, **options: object,  # kwargs-ok: transparent counting wrapper around the Redis SDK boundary
    ) -> object:
        self.calls += 1
        if not self.available:
            raise RedisConnectionError("fixture transport unavailable")
        return await super().execute_command(*args, **options)


class KeyServer:
    def __init__(self) -> None:
        self.keys = tuple(generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
        self.port = 0
        self.requests = 0
        self.number = 0
        self.mode = "healthy"
        self.delay = 0.0

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def rotate(self, number: int) -> None:
        self.number = number

    def respond(self, mode: str) -> None:
        self.mode = mode

    def delay_for(self, seconds: float) -> None:
        self.delay = seconds

    def body(self) -> bytes:
        key: Final = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[self.number].public_key())),
                      "kid": str(self.number)}
        if self.mode == "oversized":
            return b"x" * 65537
        if self.mode == "private":
            return json.dumps({"keys": [{**key, "d": "private-rsa-component"}]}).encode()
        if self.mode == "duplicate":
            return ('{"keys":[' + json.dumps(key)[:-1] + ',"kid":"' + str(self.number) + '"}]}').encode()
        if self.mode == "duplicate-keys":
            return json.dumps({"keys": [key, key]}).encode()
        if self.mode == "too-many-keys":
            return json.dumps({"keys": [{**key, "kid": str(number)} for number in range(17)]}).encode()
        if self.mode == "weak":
            weak: Final = generate_private_key(public_exponent=65537, key_size=1024)
            return json.dumps({"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key())),
                                        "kid": "weak"}]}).encode()
        return json.dumps({"keys": [key]}).encode()

    def issue(self, settings: ApiJwtConfig, *, kid: str | None = None) -> SecretStr:
        now: Final = int(time.time())
        return SecretStr(jwt.encode({
            "iss": settings.issuer, "sub": settings.bindings[0].subject, "aud": settings.audience,
            "iat": now, "exp": now + 120, "client_id": settings.client_id,
            "jti": "private-reusable-jti", "scope": settings.required_scope,
            "gateway_revision": str(settings.bindings[0].binding_revision), "role": "proxy_admin",
        }, self.keys[self.number], algorithm="RS256", headers={
            "kid": str(self.number) if kid is None else kid, "typ": "at+jwt",
        }))


@dataclass(slots=True)
class Clock:
    value: float

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def server() -> Iterator[KeyServer]:
    service: Final = KeyServer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            service.requests += 1
            time.sleep(service.delay)
            if service.mode == "redirect":
                self.send_response(307)
                self.send_header("Location", service.origin + "/untrusted")
                self.end_headers()
                return
            body: Final = service.body()
            self.send_response(503 if service.mode == "offline" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    http: Final = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    service.port = http.server_port
    thread: Final = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def config(server: KeyServer, *, namespace: str | None = None, generation: int = 1) -> ApiJwtConfig:
    return ApiJwtConfig.model_validate({
        "namespace": namespace or uuid4().hex, "generation": generation, "issuer": server.origin,
        "jwks_uri": server.origin + "/jwks", "audience": "gateway-api", "client_id": "private-client",
        "bindings": [{"subject": "private-subject", "user_id": "local-owner", "token_hash": "a" * 64,
                      "binding_revision": str(uuid4())}], "allow_loopback_http": True,
    })


@pytest.mark.asyncio
async def test_cross_worker_shared_keys_and_local_hot_verification_issue_no_rpc(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    first: Final = ApiJwtRuntime(settings, redis)
    second: Final = ApiJwtRuntime(settings, redis)
    try:
        await first.start()
        await second.start()
        assert first.status() and second.status() and server.requests == 1
        before: Final = redis.calls
        encoded: Final = server.issue(settings)
        verified: Final = await asyncio.gather(*(runtime.verify(encoded) for runtime in (first, second) * 50))
        assert all(isinstance(result, VerifiedApiJwt) for result in verified)
        assert all("private-" not in repr(result) for result in verified)
        assert redis.calls == before and server.requests == 1
    finally:
        await first.close()
        await second.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_unknown_kid_recovers_once_for_both_workers_and_storm_is_bounded(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    first: Final = ApiJwtRuntime(settings, redis)
    second: Final = ApiJwtRuntime(settings, redis)
    try:
        await first.start()
        await second.start()
        await redis.delete(f"retail:api-jwt:{settings.namespace}:{settings.digest()}:cooldown")
        server.rotate(1)
        encoded: Final = server.issue(settings)
        results: Final = await asyncio.gather(*(runtime.verify(encoded) for runtime in (first, second) * 10))
        assert all(isinstance(result, VerifiedApiJwt) for result in results)
        assert server.requests == 2
        unknown: Final = tuple(server.issue(settings, kid=f"missing-{number}") for number in range(50))
        calls: Final = redis.calls
        rejected: Final = await asyncio.gather(*(first.verify(value) for value in unknown))
        assert all(isinstance(value, ApiJwtFailure) for value in rejected)
        assert server.requests == 2 and redis.calls - calls <= 10
    finally:
        await first.close()
        await second.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_hard_expiry_never_uses_stale_keys_or_fetches_on_normal_request(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    clock: Final = Clock(time.time())
    runtime: Final = ApiJwtRuntime(settings, redis, clock=clock)
    try:
        await runtime.start()
        assert runtime.status()
        ttl: Final = await redis.ttl(f"retail:api-jwt:{settings.namespace}:{settings.digest()}:snapshot")
        assert 0 < ttl <= 300
        clock.advance(301)
        calls: Final = redis.calls
        assert not runtime.status()
        assert await runtime.verify(server.issue(settings)) == ApiJwtFailure("unavailable")
        assert redis.calls == calls and server.requests == 1
    finally:
        await runtime.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_stale_generation_and_same_generation_different_config_cannot_activate(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server, generation=2)
    current: Final = ApiJwtRuntime(settings, redis)
    stale: Final = ApiJwtRuntime(settings.model_copy(update={"generation": 1}), redis)
    conflicting: Final = ApiJwtRuntime(settings.model_copy(update={"client_id": "other"}), redis)
    try:
        await current.start()
        await stale.start()
        await conflicting.start()
        assert current.status() and not stale.status() and not conflicting.status()
        assert await stale.verify(server.issue(settings)) == ApiJwtFailure("unavailable")
        assert await conflicting.verify(server.issue(settings)) == ApiJwtFailure("unavailable")
        assert server.requests == 1
    finally:
        await current.close()
        await stale.close()
        await conflicting.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", (
    "redirect", "oversized", "private", "duplicate", "duplicate-keys", "too-many-keys", "weak", "offline",
))
async def test_untrusted_or_unavailable_jwks_never_warms_authorization(server: KeyServer, mode: str) -> None:
    redis: Final = CountingRedis()
    server.respond(mode)
    runtime: Final = ApiJwtRuntime(config(server), redis)
    try:
        await runtime.start()
        assert not runtime.status() and server.requests == 1
    finally:
        await runtime.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_unknown_kid_cannot_override_the_global_refresh_cooldown(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    runtime: Final = ApiJwtRuntime(settings, redis)
    try:
        await runtime.start()
        assert await runtime.verify(server.issue(settings, kid="unknown")) == ApiJwtFailure("invalid")
        assert server.requests == 1
    finally:
        await runtime.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_replaced_lease_owner_cannot_publish_or_unlock_the_replacement(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    runtime: Final = ApiJwtRuntime(settings, redis)
    server.delay_for(0.2)
    lease: Final = f"retail:api-jwt:{settings.namespace}:{settings.digest()}:lease"
    try:
        starting: Final = asyncio.create_task(runtime.start())
        while server.requests == 0:
            await asyncio.sleep(0.01)
        await redis.set(lease, "replacement-owner", px=7000)
        await starting
        assert not runtime.status()
        assert await redis.get(lease) == b"replacement-owner"
    finally:
        await runtime.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_refresh_lease_owner_cannot_publish_after_generation_changes(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    old: Final = ApiJwtRuntime(settings, redis)
    new: Final = ApiJwtRuntime(settings.model_copy(update={"generation": 2}), redis)
    server.delay_for(0.2)
    try:
        starting: Final = asyncio.create_task(old.start())
        while server.requests == 0:
            await asyncio.sleep(0.01)
        await new.start()
        await starting
        assert new.status() and not old.status()
    finally:
        await old.close()
        await new.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_initial_redis_outage_recovers_in_background_without_restart(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    runtime: Final = ApiJwtRuntime(config(server), redis)
    redis.set_available(False)
    try:
        await runtime.start()
        assert not runtime.status() and server.requests == 0
        redis.set_available(True)
        async with asyncio.timeout(7):
            while not runtime.status():
                await asyncio.sleep(0.05)
        assert server.requests == 1
    finally:
        redis.set_available(True)
        await runtime.close()
        await redis.aclose(close_connection_pool=True)


@pytest.mark.asyncio
async def test_unknown_kid_fetch_does_not_hold_requests_or_cancel_the_shared_refresh(server: KeyServer) -> None:
    redis: Final = CountingRedis()
    settings: Final = config(server)
    runtime: Final = ApiJwtRuntime(settings, redis)
    try:
        await runtime.start()
        await redis.delete(f"retail:api-jwt:{settings.namespace}:{settings.digest()}:cooldown")
        server.rotate(1)
        server.delay_for(0.8)
        started: Final = time.monotonic()
        assert await runtime.verify(server.issue(settings)) == ApiJwtFailure("unavailable")
        assert time.monotonic() - started < 0.75
        await asyncio.sleep(0.5)
        assert isinstance(await runtime.verify(server.issue(settings)), VerifiedApiJwt)
        assert server.requests == 2
    finally:
        await runtime.close()
        await redis.aclose(close_connection_pool=True)

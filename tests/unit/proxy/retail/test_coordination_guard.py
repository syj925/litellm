import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from litellm.caching.dual_cache import DualCache
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.retail.coordination_guard import (
    CoordinationGuardMiddleware,
    CoordinationMonitor,
    GuardConfig,
    UnsupportedAuthCache,
    UnsupportedReadReplica,
    carries_credentials,
    reset_local_auth_objects,
    validate_cache_configuration,
)


@dataclass
class Authority:
    now: float = 100
    healthy: bool = True
    calls: int = 0
    resets: int = 0

    async def probe(self) -> bool:
        self.calls += 1
        return self.healthy

    async def recover(self) -> None:
        self.resets += 1

    def clock(self) -> float:
        return self.now


def monitor(authority: Authority) -> CoordinationMonitor:
    return CoordinationMonitor(
        GuardConfig(required=True),
        probe=authority.probe,
        recover=authority.recover,
        clock=authority.clock,
        credential_headers=lambda: frozenset({b"authorization", b"qa-key"}),
    )


async def upstream(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"upstream"})


@pytest.mark.asyncio
async def test_unknown_failed_stale_and_recovered_health_require_local_auth_reset() -> None:
    authority: Final = Authority()
    health: Final = monitor(authority)
    assert health.status().state == "unknown" and health.accepting() is False
    await health.check_once()
    assert health.accepting() is True and authority.resets == 1
    await health.check_once()
    assert authority.resets == 1
    authority.now = 103
    assert health.status().state == "stale" and health.accepting() is False
    await health.check_once()
    assert health.accepting() is True and authority.resets == 2
    authority.healthy = False
    await health.check_once()
    assert health.status().state == "unhealthy" and health.accepting() is False
    authority.healthy = True
    await health.check_once()
    assert health.accepting() is True and authority.resets == 3


@pytest.mark.asyncio
async def test_fresh_health_reuses_snapshot_without_request_time_redis_calls() -> None:
    authority: Final = Authority()
    health: Final = monitor(authority)
    await health.check_once()
    app: Final = CoordinationGuardMiddleware(upstream, monitor=health, config=health.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(50):
            response: Final = await client.get("/v1/models", headers={"Authorization": "Bearer fixture"})
            assert response.status_code == 200
    assert authority.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,headers",
    (
        ("/retail/wallet", {"Authorization": "Bearer fixture"}),
        ("/v1/chat/completions", {"x-litellm-api-key": "fixture"}),
        ("/key/generate", {"API-Key": "fixture"}),
        ("/v1/models", {"x-api-key": "fixture"}),
        ("/v1/models", {"x-goog-api-key": "fixture"}),
        ("/retail/reports", {"Ocp-Apim-Subscription-Key": "fixture"}),
        ("/retail/wallet", {"litellm_user_api_key": "fixture"}),
        ("/retail/wallet", {"x-mcp-auth": "fixture"}),
        ("/health/readiness/details", {"Cookie": "other=ignored; token=fixture"}),
        ("/v1beta/models/model:generateContent?key=fixture", {}),
        ("/v1beta/models/model:streamGenerateContent?%6b%65%79=fixture", {}),
        ("/v1/models?api_key=fixture", {}),
        ("/v1/models?access_token=fixture", {}),
    ),
)
async def test_unknown_coordination_rejects_every_native_credential_transport_privately(
    path: str,
    headers: Mapping[str, str],
) -> None:
    app: Final = CoordinationGuardMiddleware(upstream, config=GuardConfig(required=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get(path, headers=headers)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["error"]["code"] == "coordination_unavailable"
    assert "fixture" not in response.text


@pytest.mark.asyncio
async def test_configured_custom_header_is_guarded_before_first_background_probe() -> None:
    authority: Final = Authority(healthy=False)
    health: Final = monitor(authority)
    app: Final = CoordinationGuardMiddleware(upstream, monitor=health, config=health.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/retail/wallet", headers={"qa-key": "fixture"})
    assert response.status_code == 503
    assert authority.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    (
        "/ui/welcome/",
        "/ui/models/",
        "/ui/login/",
        "/ui/assets/logo.svg",
        "/ui/_next/static/app.js",
        "/health/liveness",
        "/health/liveliness",
        "/favicon.ico",
    ),
)
async def test_public_static_and_exact_health_paths_remain_accessible_with_auth_cookie(path: str) -> None:
    app: Final = CoordinationGuardMiddleware(upstream, config=GuardConfig(required=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get(path, headers={"Cookie": "token=fixture"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_password_login_is_exact_exemption_and_no_broad_retail_or_health_details_exemption() -> None:
    app: Final = CoordinationGuardMiddleware(upstream, config=GuardConfig(required=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login: Final = await client.post("/login", headers={"Cookie": "token=fixture"})
        details: Final = await client.get("/health/readiness/details", headers={"Authorization": "Bearer fixture"})
        retail: Final = await client.post("/retail/keys/rotate", headers={"Authorization": "Bearer fixture"})
        anonymous: Final = await client.get("/v1/models")
    assert login.status_code == 200 and anonymous.status_code == 200
    assert details.status_code == 503 and retail.status_code == 503


@pytest.mark.asyncio
async def test_readiness_tracks_unknown_down_and_recovery_while_liveness_stays_accessible() -> None:
    authority: Final = Authority()
    health: Final = monitor(authority)
    app: Final = CoordinationGuardMiddleware(upstream, monitor=health, config=health.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/readiness")).status_code == 503
        assert (await client.get("/health/liveliness")).status_code == 200
        await health.check_once()
        assert (await client.get("/health/readiness")).status_code == 200
        authority.healthy = False
        await health.check_once()
        assert (await client.get("/health/readiness")).status_code == 503
        authority.healthy = True
        await health.check_once()
        assert (await client.get("/health/readiness")).status_code == 200


@pytest.mark.asyncio
async def test_guard_is_opt_in_and_does_not_modify_legacy_request_handling() -> None:
    app: Final = CoordinationGuardMiddleware(upstream, config=GuardConfig())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer fixture"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_websocket_credentials_close_new_handshake_without_reading_body_or_connect_event() -> None:
    messages: Final[list[Message]] = []  # mutable-ok: this owned ASGI send capture appends received frames

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        raise AssertionError("Unavailable coordination must deny before reading from the client")

    scope: Final[Scope] = {
        "type": "websocket",
        "path": "/v1/realtime",
        "query_string": b"",
        "headers": [(b"sec-websocket-protocol", b"realtime,openai-insecure-api-key.fixture")],
    }
    app: Final = CoordinationGuardMiddleware(upstream, config=GuardConfig(required=True))
    await app(scope, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1013, "reason": "Gateway coordination unavailable"}]


def test_recovery_clears_only_native_local_authorization_partitions_and_preserves_financial_counter() -> None:
    auth: Final = UserApiKeyCache()
    spend: Final = DualCache()
    auth.in_memory_cache.set_cache("user", {"user_role": "proxy_admin"})
    auth.key_object_cache.in_memory_cache.set_cache("a" * 64, {"user_id": "owner"})
    spend.in_memory_cache.set_cache("budget-counter", 17)
    reset_local_auth_objects(auth)
    assert auth.in_memory_cache.get_cache("user") is None
    assert auth.key_object_cache.in_memory_cache.get_cache("a" * 64) is None
    assert spend.in_memory_cache.get_cache("budget-counter") == 17


@pytest.mark.parametrize(
    "updates",
    (
        {"interval": 0},
        {"interval": 1},
        {"timeout": 1},
        {"max_age": 3},
        {"max_age": 0.1},
        {"interval": float("nan")},
        {"timeout": float("inf")},
    ),
)
def test_guard_timing_rejects_unbounded_or_nonfinite_configuration(updates: Mapping[str, float]) -> None:
    with pytest.raises(ValueError):
        GuardConfig(**updates)


def test_guard_boolean_rejects_silent_security_configuration_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_REQUIRE_COORDINATION_REDIS", "ture")
    with pytest.raises(ValueError, match="true or false"):
        GuardConfig.from_environment()


def test_oversized_credential_metadata_cannot_bypass_bounded_parsing() -> None:
    names: Final = frozenset({b"authorization"})
    assert carries_credentials([(b"cookie", b"a=b;" * 100 + b"token=fixture")], b"", names)
    assert carries_credentials([], b"a=b&" * 300 + b"key=fixture", names)
    assert carries_credentials([], b"a" * 8193, names)


def test_strict_recovery_rejects_replica_reads_even_when_authorization_cache_is_local() -> None:
    cache: Final = UserApiKeyCache()
    validate_cache_configuration(cache)
    with pytest.raises(UnsupportedReadReplica, match="READ_REPLICA"):
        validate_cache_configuration(cache, read_replica=True)


@pytest.mark.parametrize("partition", ("object", "key"))
def test_strict_recovery_rejects_redis_backed_authorization_partitions(
    partition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache: Final = UserApiKeyCache()
    target: Final = cache if partition == "object" else cache.key_object_cache
    monkeypatch.setattr(target, "redis_cache", object())
    with pytest.raises(UnsupportedAuthCache, match="enable_redis_auth_cache=false"):
        validate_cache_configuration(cache)


@pytest.mark.asyncio
async def test_slow_recovery_reset_does_not_refresh_an_old_successful_probe_timestamp() -> None:
    authority: Final = Authority()

    async def recover() -> None:
        authority.now += 3

    health: Final = CoordinationMonitor(
        GuardConfig(required=True),
        probe=authority.probe,
        recover=recover,
        clock=authority.clock,
        credential_headers=lambda: frozenset(),
    )
    await health.check_once()
    assert health.status().state == "stale" and health.accepting() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,headers",
    (
        ("POST", "/v1/chat/completions", {"Authorization": "Bearer fixture"}),
        ("GET", "/ui/welcome/", {}),
        ("POST", "/login", {}),
        ("GET", "/qa/anonymous", {}),
    ),
)
async def test_pre_recovery_request_cannot_refill_real_auth_cache_after_generation_changes(
    method: str,
    path: str,
    headers: Mapping[str, str],
) -> None:
    authority: Final = Authority()
    cache: Final = UserApiKeyCache()

    async def recover() -> None:
        reset_local_auth_objects(cache)

    health: Final = CoordinationMonitor(
        GuardConfig(required=True),
        probe=authority.probe,
        recover=recover,
        clock=authority.clock,
        credential_headers=lambda: frozenset({b"authorization"}),
    )
    await health.check_once()
    cache.set_write_guard(health.cache_writes_allowed)
    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()
    key: Final = "a" * 64

    async def handler(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"] == "/v1/models":
            await cache.async_set_cache(key, {"authorization": "current"})
        else:
            entered.set()
            await release.wait()
            await cache.async_set_cache(key, {"authorization": "stale"})
            cache.set_cache("role-owner", {"user_role": "proxy_admin"})
        await upstream(scope, receive, send)

    app: Final = CoordinationGuardMiddleware(handler, monitor=health, config=health.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        old: Final = asyncio.create_task(client.request(method, path, headers=headers))
        await entered.wait()
        generation: Final = health.generation
        authority.healthy = False
        await health.check_once()
        assert health.generation is not generation
        authority.healthy = True
        await health.check_once()
        fresh: Final = await client.get("/v1/models", headers={"Authorization": "Bearer fixture"})
        assert fresh.status_code == 200
        release.set()
        assert (await old).status_code == 200
    assert cache.get_cache(key) == {"authorization": "current"}
    assert cache.get_cache("role-owner") is None
    assert health.cache_writes_allowed() is False
    cache.set_cache("background-role", {"user_role": "proxy_admin"})
    assert cache.get_cache("background-role") is None


@pytest.mark.asyncio
async def test_child_task_inherits_old_generation_after_request_context_is_reset() -> None:
    authority: Final = Authority()
    health: Final = monitor(authority)
    await health.check_once()
    cache: Final = UserApiKeyCache()
    cache.set_write_guard(health.cache_writes_allowed)
    release: Final = asyncio.Event()
    finished: Final = asyncio.Event()

    async def late_write() -> None:
        await release.wait()
        await cache.async_set_cache_pipeline((("a" * 64, {"state": "stale"}), ("owner-role", {"role": "stale"})))
        finished.set()

    async def handler(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"] == "/login":
            asyncio.create_task(late_write())
        else:
            await cache.async_set_cache_pipeline((("a" * 64, {"state": "current"}),))
        await upstream(scope, receive, send)

    app: Final = CoordinationGuardMiddleware(handler, monitor=health, config=health.config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/login")).status_code == 200
        authority.healthy = False
        await health.check_once()
        authority.healthy = True
        await health.check_once()
        assert (await client.get("/v1/models", headers={"Authorization": "Bearer fixture"})).status_code == 200
        release.set()
        await finished.wait()
    assert cache.get_cache("a" * 64) == {"state": "current"}
    assert cache.get_cache("owner-role") is None

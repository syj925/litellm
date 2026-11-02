import os
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.retail.api import retail_store, router
from litellm.proxy.retail.billing import RetailBilling
from litellm.proxy.retail.request_tracing import TraceCollector, activate_trace, bind_call_id, reset_trace
from litellm.proxy.retail.store import WalletStore
from litellm.proxy.retail.trace_store import cleanup_trace_bodies, get_request_trace, persist_trace


@pytest.mark.asyncio
async def test_request_traces_preserve_frozen_billing_and_enforce_body_and_record_retention() -> None:
    database_url: Final = os.environ.get("RETAIL_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_trace_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        await _assert_trace_storage(dsn)
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _assert_trace_storage(database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
    store: Final = WalletStore(database_url)
    await store.initialize()
    now: Final = datetime.now(timezone.utc)
    await _assert_late_body_persistence_keeps_newer_trace(store, now)
    snapshot: Final = {
        "schema_version": 1, "trace_id": "server-trace", "call_id": "call", "retail_request_id": "reservation",
        "started_at": now.isoformat(),
        "phases": [{"name": "auth", "duration_ms": 2.5}], "attempts": [],
    }
    await persist_trace(snapshot, {"api_key": "secret-key", "messages": [{"content": "visible for diagnostics"}]}, store=store)
    await store.get_wallet("user", create=True)
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_wallets SET balance_nanos=1000000000 WHERE user_id='user'")
    frozen: Final = {"price_version": 7, "discount": "0.7", "resource_pool_id": "original-pool"}
    await store.reserve("user", "reservation", "model", "0.01", frozen)
    await store.settle("reservation", "0.001", {
        "gateway_call_id": "call", "usage": {"input_tokens": 10}, "provider_cost": "0.0005",
    })
    result: Final = await get_request_trace(store, "call")
    assert result["recorded"] is True
    assert result["trace"]["phases"] == snapshot["phases"]
    assert result["billing"]["price_snapshot"] == frozen
    assert result["billing"]["usage"] == {"input_tokens": 10}
    assert result["billing"]["provider_cost_source"] == "gateway_cost_calculation"
    assert "secret-key" not in str(result)
    assert result["request_body"]["available"] is True
    server_lookup: Final = await get_request_trace(store, "server-trace")
    assert server_lookup["trace"] == result["trace"]
    assert server_lookup["billing"] == result["billing"]
    await persist_trace({"trace_id": "auth-failure", "call_id": None, "started_at": now.isoformat()}, store=store)
    auth_failure: Final = await get_request_trace(store, "auth-failure")
    assert auth_failure["recorded"] is True
    assert auth_failure["trace"]["call_id"] is None
    assert "native_log_link" in auth_failure["missing"]
    assert auth_failure["billing"] is None
    late_snapshot: Final = {**snapshot, "duration_ms": 50, "events": [{"name": "billing.settled", "at_ms": 50}]}
    await persist_trace(late_snapshot, store=store)
    await persist_trace({**snapshot, "duration_ms": 10}, {"messages": ["stale callback body"]}, store=store)
    late: Final = await get_request_trace(store, "call")
    assert late["trace"]["duration_ms"] == 50
    assert late["trace"]["events"] == late_snapshot["events"]
    assert late["request_body"] == result["request_body"]
    historical: Final = await get_request_trace(store, "no-record")
    assert historical["recorded"] is False and historical["trace"] is None
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_request_traces SET body_expires_at=now()-interval '1 second' WHERE call_id='call'")
    await persist_trace({**late_snapshot, "duration_ms": 60}, {"messages": ["must not revive expired body"]}, store=store)
    expired: Final = await get_request_trace(store, "call")
    assert expired["request_body"]["available"] is False
    assert expired["request_body"]["value"] is None
    assert expired["billing"]["price_snapshot"] == frozen
    async with store.transaction() as connection:
        cursor: Final = await connection.execute("SELECT request_body FROM retail_request_traces WHERE call_id='call'")
        erased: Final = await cursor.fetchone()
    assert erased is not None and erased["request_body"] is None
    await _assert_admin_trace_endpoint(store)
    await _assert_late_billing_persistence(store)
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_request_traces SET created_at=%s WHERE call_id='call'", (now - timedelta(days=31),))
    await cleanup_trace_bodies(store)
    deleted: Final = await get_request_trace(store, "call")
    assert deleted["recorded"] is False
    assert deleted["billing"]["price_snapshot"] == frozen
    await _assert_reused_native_call_ids_keep_bodies_and_billing_separate(store, now)


async def _assert_late_body_persistence_keeps_newer_trace(store: WalletStore, now: datetime) -> None:
    identity: Final = {"trace_id": "body-race-server", "call_id": "body-race-call", "started_at": now.isoformat()}
    newer: Final = {**identity, "duration_ms": 210, "events": [{"name": "billing.settled", "at_ms": 210}]}
    older: Final = {**identity, "duration_ms": 200, "events": [{"name": "response_complete", "at_ms": 200}]}
    await persist_trace(newer, store=store)
    before_body: Final = await get_request_trace(store, "body-race-server")
    assert before_body["request_body"]["available"] is False
    await persist_trace(older, {"messages": ["initial middleware body"], "token": "private-token"}, store=store)
    merged: Final = await get_request_trace(store, "body-race-server")
    assert merged["trace"] == newer
    assert merged["request_body"]["available"] is True
    assert merged["request_body"]["value"] == {"messages": ["initial middleware body"], "token": "[REDACTED]"}
    assert merged["request_body"]["expires_at"] == before_body["request_body"]["expires_at"]
    await persist_trace(older, {"messages": ["must not replace the first body"]}, store=store)
    repeated: Final = await get_request_trace(store, "body-race-server")
    assert repeated["trace"] == newer
    assert repeated["request_body"] == merged["request_body"]


async def _assert_reused_native_call_ids_keep_bodies_and_billing_separate(store: WalletStore, now: datetime) -> None:
    await store.reserve("user", "collision-reservation", "model", "0.01", {"price_version": 8})
    await store.settle("collision-reservation", "0.001", {"gateway_call_id": "shared-call"})
    await persist_trace({
        "trace_id": "first-server-trace", "call_id": "shared-call", "started_at": now.isoformat(),
        "retail_request_id": "collision-reservation", "duration_ms": 100,
    }, {"messages": ["first body"]}, store=store)
    await persist_trace({
        "trace_id": "second-server-trace", "call_id": "shared-call", "started_at": now.isoformat(), "duration_ms": 10,
    }, {"messages": ["second body"]}, store=store)
    first: Final = await get_request_trace(store, "first-server-trace")
    second: Final = await get_request_trace(store, "second-server-trace")
    latest: Final = await get_request_trace(store, "shared-call")
    assert first["request_body"]["value"] == {"messages": ["first body"]}
    assert first["billing"]["retail_request_id"] == "collision-reservation"
    assert second["request_body"]["value"] == {"messages": ["second body"]}
    assert second["billing"] is None
    assert latest["trace"]["trace_id"] == "second-server-trace"
    assert latest["request_body"] == second["request_body"]
    assert latest["billing"] is None
    assert "ambiguous_call_id" in latest["missing"]
    assert "ambiguous_call_id" not in first["missing"]


async def _assert_late_billing_persistence(store: WalletStore) -> None:
    await store.reserve("user", "late-reservation", "model", "0.01", {
        "version": "token-v3", "discount": "0.7", "input_cost_per_token": "0.0001",
        "output_cost_per_token": "0.0002", "cache_read_input_token_cost": "0", "cache_creation_input_token_cost": "0",
    })
    trace: Final = TraceCollector("POST", "/v1/chat/completions")
    token: Final = activate_trace(trace)
    try:
        bind_call_id("late-call")
        trace.milestone("response_complete")
        trace.complete()
        await persist_trace(trace.snapshot(), {"messages": ["initial diagnostic body"]}, store=store)
        await RetailBilling(store).async_post_call_success_hook(
            {"litellm_call_id": "late-call", "metadata": {"retail_request_id": "late-reservation"}},
            UserAPIKeyAuth(user_id="user", user_role=LitellmUserRoles.INTERNAL_USER),
            {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        )
    finally:
        reset_trace(token)
    result: Final = await get_request_trace(store, "late-call")
    assert result["trace"]["retail_request_id"] == "late-reservation"
    assert any(phase["name"] == "billing.settle" for phase in result["trace"]["phases"])
    assert any(event["name"] == "billing.settled" for event in result["trace"]["events"])
    assert result["billing"]["status"] == "settled"
    assert result["request_body"]["value"] == {"messages": ["initial diagnostic body"]}


async def _assert_admin_trace_endpoint(store: WalletStore) -> None:
    async def administrator() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN)

    async def storage() -> WalletStore:
        return store

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = administrator
    app.dependency_overrides[retail_store] = storage
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://trace.test") as client:
        response: Final = await client.get("/retail/admin/request-traces/call")
    assert response.status_code == 200, response.text
    assert response.json()["billing"]["retail_request_id"] == "reservation"
    assert response.json()["request_body"]["value"] is None

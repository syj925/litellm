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
from psycopg.types.json import Jsonb

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.api import retail_store, retail_user
from litellm.proxy.retail.cache_usage_metrics import CacheUsageQuery, load_cache_usage_metrics, router
from litellm.proxy.retail.store import WalletStore


@pytest.mark.asyncio
async def test_cache_usage_global_and_filtered_scopes_preserve_ttls_and_exact_observation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url: Final = os.environ.get("RETAIL_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_cache_usage_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        await _assert_cache_usage_metrics(dsn, monkeypatch)
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _assert_cache_usage_metrics(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    now: Final = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    started: Final = now.replace(tzinfo=None)
    store: Final = WalletStore(database_url)
    async with store.transaction() as connection:
        await connection.execute(
            'CREATE TABLE "LiteLLM_SpendLogs"('
            'request_id TEXT PRIMARY KEY,litellm_call_id TEXT,"user" TEXT,"startTime" TIMESTAMP(3),'
            'api_key TEXT,team_id TEXT,model TEXT,model_group TEXT,metadata JSONB)'
        )
        await connection.execute(
            'CREATE TABLE retail_cache_observations('
            'litellm_call_id TEXT,user_id TEXT,started_at TIMESTAMP(3),cache_read_tokens BIGINT,'
            'cache_write_tokens BIGINT,cache_write_ttl_tokens JSONB,cache_write_unclassified_tokens BIGINT,'
            'PRIMARY KEY(litellm_call_id,user_id,started_at))'
        )
        for request_id, owner, key, team, model, group, metadata in (
            ("owner", "owner", "key-a", "team-a", "vendor/one", "public", {}),
            ("other", "other", "key-b", "team-b", "vendor/two", "public", {}),
            ("historical", "owner", "key-a", "team-a", "vendor/one", "public", {
                "additional_usage_values": {"cache_read_input_tokens": 2, "cache_creation_input_tokens": 7},
            }),
            ("wrong-owner", "owner", "key-a", "team-a", "vendor/one", "public", {
                "additional_usage_values": {"cache_read_input_tokens": 3, "cache_creation_input_tokens": 9},
            }),
            ("wrong-start", "owner", "key-a", "team-a", "vendor/one", "public", {
                "additional_usage_values": {"cache_read_input_tokens": 4, "cache_creation_input_tokens": 11},
            }),
            ("missing", "owner", "key-a", "team-a", "vendor/one", "public", {}),
            ("helper-attempt-1", "owner", "key-a", "team-a", "vendor/one", "public", {}),
        ):
            await connection.execute(
                'INSERT INTO "LiteLLM_SpendLogs" VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (request_id, request_id, owner, started, key, team, model, group, Jsonb(metadata)),
            )
        for call_id, owner, stamp, read, write, tiers, unspecified in (
            ("owner", "owner", started, 10, 100, {"300": 20, "750": 10, "3600": 30}, 40),
            ("other", "other", started, 20, 200, {"1800": 50, "43200": 100}, 50),
            ("wrong-owner", "unrelated", started, 9000, 9000, {"300": 9000}, 0),
            ("wrong-start", "owner", started + timedelta(milliseconds=1), 11000, 11000, {"300": 11000}, 0),
            ("helper-attempt-1", "owner", started, 1000, 1000, {"300": 1000}, 0),
        ):
            await connection.execute(
                "INSERT INTO retail_cache_observations VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (call_id, owner, stamp, read, write, Jsonb(tiers), unspecified),
            )
    query: Final = CacheUsageQuery(start_date=now - timedelta(hours=1), end_date=now + timedelta(hours=1))
    global_metrics: Final = await load_cache_usage_metrics(store, query, None)
    assert global_metrics.request_count == 6
    assert global_metrics.cache_usage_samples == 5
    assert global_metrics.cache_usage_unknown_requests == 1
    assert global_metrics.cache_read_tokens == 39
    assert global_metrics.cache_write_tokens == 327
    assert global_metrics.cache_write_ttl_tokens == {"300": 20, "750": 10, "3600": 30, "1800": 50, "43200": 100}
    assert global_metrics.cache_write_unclassified_tokens == 117
    assert global_metrics.cache_write_tokens == (
        sum(global_metrics.cache_write_ttl_tokens.values()) + global_metrics.cache_write_unclassified_tokens
    )
    own: Final = await load_cache_usage_metrics(store, query, "owner")
    assert own.request_count == 5
    assert own.cache_write_tokens == 127
    assert own.cache_write_ttl_tokens == {"300": 20, "750": 10, "3600": 30}
    assert own.cache_write_unclassified_tokens == 67
    for selected in (
        query.model_copy(update={"api_key": "key-b"}), query.model_copy(update={"team_id": "team-b"}),
        query.model_copy(update={"model": "vendor/two"}), query.model_copy(update={"models": ("vendor/two",)}),
    ):
        result: Final = await load_cache_usage_metrics(store, selected, None)
        assert result.request_count == 1
        assert result.cache_write_tokens == 200
        assert result.cache_write_ttl_tokens == {"1800": 50, "43200": 100}
    grouped: Final = await load_cache_usage_metrics(store, query.model_copy(update={"model_groups": ("public",)}), None)
    assert grouped == global_metrics
    absent: Final = await load_cache_usage_metrics(store, query.model_copy(update={"team_id": "absent"}), None)
    assert absent.request_count == 0
    assert absent.cache_write_tokens is None
    assert absent.cache_write_ttl_tokens == {}
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "false")
    await _assert_scoped_endpoint(store, now, {"models": ["vendor/one"], "model_groups": ["public"]}, 127)
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    await _assert_scoped_endpoint(
        store, now, {"model": "public", "models": ["public"], "model_groups": ["public"]}, 127,
    )
    await _assert_scoped_endpoint(store, now, {"models": ["public"], "model_groups": ["different"]}, None)
    await _assert_legacy_cache_splits(store, now)


async def _assert_legacy_cache_splits(store: WalletStore, now: datetime) -> None:
    async with store.transaction() as connection:
        for request_id, usage in (
            ("legacy-anthropic", {
                "cache_read_input_tokens": 1, "cache_creation_input_tokens": 23,
                "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 11},
            }),
            ("legacy-openai", {"prompt_tokens_details": {
                "cached_tokens": 2, "cache_write_tokens": 31,
                "cache_write_tokens_details": {"ttl_30m_tokens": 13, "ttl_12h_tokens": 17},
            }}),
            ("legacy-splits-only", {"input_tokens_details": {
                "cached_tokens": 3, "cache_write_tokens_details": {"ttl_30m_tokens": 2, "ttl_12h_tokens": 4},
            }}),
            ("legacy-inconsistent", {
                "cache_read_input_tokens": 4, "cache_creation_input_tokens": 7,
                "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 6},
            }),
            ("legacy-zero", {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ):
            await connection.execute(
                'INSERT INTO "LiteLLM_SpendLogs" VALUES '
                "(%s,%s,'owner',%s,'history-key','history-team','vendor/legacy','legacy-public',%s)",
                (request_id, request_id, now.replace(tzinfo=None), Jsonb({"additional_usage_values": usage})),
            )
    query: Final = CacheUsageQuery(
        start_date=now - timedelta(hours=1), end_date=now + timedelta(hours=1), api_key="history-key",
    )
    result: Final = await load_cache_usage_metrics(store, query, "owner")
    assert result.request_count == result.cache_usage_samples == 5
    assert result.cache_usage_unknown_requests == 0
    assert result.cache_read_tokens == 10
    assert result.cache_write_tokens == 67
    assert result.cache_write_ttl_tokens == {"300": 5, "3600": 11, "1800": 15, "43200": 21}
    assert result.cache_write_unclassified_tokens == 15


async def _assert_scoped_endpoint(
    store: WalletStore, now: datetime, filters: dict[str, str | list[str]], expected_write: int | None,
) -> None:
    async def own_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER)

    async def existing_store() -> WalletStore:
        return store

    app: Final = FastAPI()
    app.include_router(router, prefix="/retail")
    app.dependency_overrides[retail_user] = own_user
    app.dependency_overrides[retail_store] = existing_store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.get("/retail/cache-usage", params={
            "start_date": (now - timedelta(hours=1)).isoformat(), "end_date": (now + timedelta(hours=1)).isoformat(),
            "scope": "self", **filters,
        })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["cache_write_tokens"] == expected_write
    assert response.json()["cache_write_ttl_tokens"] == (
        {"300": 20, "750": 10, "3600": 30} if expected_write is not None else {}
    )

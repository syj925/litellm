import os
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail.cache_observations import (
    CacheObservation,
    enrich_cache_logs,
    legacy_cache_sql,
    persist_cache_observation,
)
from litellm.proxy.retail.channel_metrics import _READ, _WRITE, _read_aggregates
from litellm.proxy.retail.model_metrics import ModelMetricHours, load_model_metrics
from litellm.proxy.retail.store import WalletStore


@pytest.mark.asyncio
async def test_cache_duration_aggregation_exact_identity_and_personal_log_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    database_url: Final = os.environ.get("RETAIL_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_cache_metrics_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        await _assert_duration_metrics(dsn)
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _assert_duration_metrics(database_url: str) -> None:
    now: Final = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    start: Final = now.replace(tzinfo=None) - timedelta(minutes=1)
    deployments: Final = ({"model_name": "example", "model_info": {"id": "published"},
                          "litellm_params": {"model": "test/example"}},)
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
    store: Final = WalletStore(database_url)
    await store.initialize()
    async with store.transaction() as connection:
        await _assert_millisecond_identity_sql(connection)
        await _assert_historical_duration_sql(connection)
        await _assert_compatible_cache_sql(connection)
        await connection.execute(
            'CREATE TABLE "LiteLLM_SpendLogs"('
            'request_id TEXT PRIMARY KEY,model TEXT,model_group TEXT,model_id TEXT,"user" TEXT,status TEXT,'
            '"startTime" TIMESTAMP(3),"endTime" TIMESTAMP(3),"completionStartTime" TIMESTAMP(3),'
            'request_duration_ms INT,prompt_tokens INT,completion_tokens INT,total_tokens INT,'
            'metadata JSONB,cache_hit TEXT,litellm_call_id TEXT,spend NUMERIC)'
        )
        await connection.execute(
            "INSERT INTO retail_resource_pools(id,name,source_kind,enabled,visibility) "
            "VALUES ('public','Public','anthropic',true,'public')"
        )
        await connection.execute(
            "INSERT INTO retail_pool_bindings(pool_id,deployment_id,model) VALUES ('public','published','example')"
        )
        await connection.execute("INSERT INTO retail_channels(id,name,pool_id) VALUES ('channel','Channel','public')")
        await connection.execute(
            "INSERT INTO retail_channel_deployments(channel_id,deployment_id,model) VALUES ('channel','published','example')"
        )
        for request_id, owner, write, stamp in (
            ("first", "owner", 100, start), ("second", "other", 200, start),
            ("historical", "owner", 7, start - timedelta(minutes=16)),
            ("wrong-owner", "owner", 9, start), ("wrong-start", "owner", 11, start),
            ("helper-attempt-1", "owner", 1000, start),
        ):
            details: Final = {
                "cache_creation_input_tokens": write, "cache_read_input_tokens": 1,
                **({"prompt_tokens_details": {"cache_creation_token_details": {
                    "ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 1,
                }}} if request_id == "historical" else {}),
            }
            await connection.execute(
                'INSERT INTO "LiteLLM_SpendLogs" VALUES '
                "(%s,'example','example','published',%s,'success',%s,%s,%s,2000,100,2,102,%s,'false',%s,0.001)",
                (request_id, owner, stamp, stamp + timedelta(seconds=2), stamp,
                 Jsonb({"additional_usage_values": details}),
                 request_id),
            )
    for request_id, owner, stamp, write, tiers, unknown in (
        ("first", "owner", start, 100, {"300": 20, "3600": 30}, 50),
        ("second", "other", start, 200, {"1800": 50, "43200": 100}, 50),
        ("wrong-owner", "unrelated", start, 9000, {"300": 9000}, 0),
        ("wrong-start", "owner", start + timedelta(milliseconds=1), 11000, {"300": 11000}, 0),
        ("helper-attempt-1", "owner", start, 1000, {"300": 1000}, 0),
    ):
        await persist_cache_observation(CacheObservation(
            litellm_call_id=request_id, user_id=owner, started_at=stamp, cache_read_tokens=2,
            cache_write_tokens=write, cache_write_ttl_tokens=tiers, cache_write_unclassified_tokens=unknown,
        ), store)
    public: Final = await load_model_metrics(store, "example", ModelMetricHours.DAY, None, now, deployments=deployments)
    assert public.summary.requests == 5
    assert public.summary.cache_write_tokens == 327
    assert public.summary.cache_write_ttl_tokens == {"300": 22, "3600": 31, "1800": 50, "43200": 100}
    assert public.summary.cache_write_unclassified_tokens == 124
    assert sum(row.cache_write_tokens or 0 for row in public.trend) == 327
    assert sum(row.cache_write_unclassified_tokens or 0 for row in public.trend) == 124
    async with store.transaction() as connection:
        channel_rows: Final = await _read_aggregates(connection, start - timedelta(hours=1), now, "channel", 15, True)
    channel: Final = next(row for row in channel_rows if row.kind == "channel")
    assert channel.requests == public.summary.requests
    assert channel.cache_write_ttl_tokens == public.summary.cache_write_ttl_tokens
    assert channel.cache_write_tokens == public.summary.cache_write_tokens
    assert channel.cache_write_unclassified_tokens == public.summary.cache_write_unclassified_tokens
    assert sum(row.cache_write_tokens or 0 for row in channel_rows if row.kind == "trend") == 327
    personal: Final = await load_model_metrics(store, "example", ModelMetricHours.DAY, "owner", now, deployments=deployments)
    assert personal.summary.requests == 4
    assert personal.summary.cache_write_tokens == 127
    assert personal.summary.cache_write_ttl_tokens == {"300": 22, "3600": 31}
    assert personal.summary.cache_write_unclassified_tokens == 74
    payload: Final = [{"request_id": "first", "startTime": start.isoformat()}, {"request_id": "second"}]
    personal_logs: Final = await enrich_cache_logs(
        payload, UserAPIKeyAuth(user_id="owner", user_role=LitellmUserRoles.INTERNAL_USER), store,
    )
    assert isinstance(personal_logs, list)
    assert personal_logs[0]["cache_usage"]["cache_write_ttl_tokens"] == {"300": 20, "3600": 30}
    assert "cache_usage" not in personal_logs[1]
    admin_logs: Final = await enrich_cache_logs(
        payload, UserAPIKeyAuth(user_id="admin", user_role=LitellmUserRoles.PROXY_ADMIN), store,
    )
    assert isinstance(admin_logs, list)
    assert admin_logs[1]["cache_usage"]["cache_write_ttl_tokens"] == {"1800": 50, "43200": 100}


async def _assert_millisecond_identity_sql(connection: psycopg.AsyncConnection) -> None:
    from litellm.proxy.retail.cache_observations import cache_observation
    from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload

    kwargs: Final = {
        "model": "precision-fixture", "litellm_call_id": "precision-call", "response_cost": 0,
        "litellm_params": {"litellm_metadata": {"user_api_key_user_id": "precision-owner"}},
    }
    response: Final = {"id": "precision-request", "usage": {
        "prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20},
    }}
    for microseconds in (499, 500, 999, 123499, 123500, 123999, 999499, 999500, 999999):
        start: Final = datetime(2026, 1, 1, 12, 0, 0, microseconds, tzinfo=timezone.utc)
        observation: Final = cache_observation(kwargs, response, start)
        assert observation is not None
        payload: Final = get_logging_payload(kwargs, response, start, start + timedelta(seconds=1))
        native_start: Final = payload["startTime"]
        assert isinstance(native_start, datetime)
        cursor: Final = await connection.execute(
            "WITH spend(call_id,owner,stamp) AS (VALUES (%s,%s,%s::timestamp(3))),"
            " observation(call_id,owner,stamp) AS (VALUES (%s,%s,%s::timestamp(3)))"
            " SELECT count(*) AS matches FROM spend s JOIN observation o"
            " ON s.call_id=o.call_id AND s.owner=o.owner AND s.stamp=o.stamp",
            (payload["litellm_call_id"], payload["user"], native_start.isoformat(),
             observation.litellm_call_id, observation.user_id, observation.started_at.isoformat()),
        )
        result: Final = await cursor.fetchone()
        assert result is not None
        assert result["matches"] == 1


async def _assert_historical_duration_sql(connection: psycopg.AsyncConnection) -> None:
    write_total: Final = "(s.metadata #>> '{additional_usage_values,cache_creation_input_tokens}')::bigint"
    cases: Final = (
        ({}, (None, {}, None)),
        ({"cache_creation_input_tokens": 7}, (7, {}, 7)),
        ({"cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_5m_input_tokens": 3}}, (3, {"300": 3}, 0)),
        ({"cache_creation": {"ephemeral_5m_input_tokens": 3}}, (3, {"300": 3}, 0)),
        ({"cache_creation_input_tokens": 7, "cache_creation": {"ephemeral_5m_input_tokens": 8}}, (7, {}, 7)),
        ({"cache_creation_input_tokens": 7, "cache_creation": {"ephemeral_5m_input_tokens": -1}}, (7, {}, 7)),
        ({"input_tokens_details": {"cache_write_tokens_details": {"ttl_30m_tokens": 2, "ttl_12h_tokens": 3}}},
         (5, {"1800": 2, "43200": 3}, 0)),
    )
    for usage, expected in cases:
        cursor: Final = await connection.execute(
            "SELECT cache_write_tokens,cache_write_ttl_tokens,cache_write_unclassified_tokens "
            f"FROM (VALUES (%s::jsonb)) s(metadata) {legacy_cache_sql(write_total)}",
            (Jsonb({"additional_usage_values": usage}),),
        )
        row: Final = await cursor.fetchone()
        assert row is not None
        assert (row["cache_write_tokens"], row["cache_write_ttl_tokens"], row["cache_write_unclassified_tokens"]) == expected


async def _assert_compatible_cache_sql(connection: psycopg.AsyncConnection) -> None:
    cases: Final = (
        ({"prompt_cache_hit_tokens": 20}, (20, None, {}, None)),
        ({"prompt_cache_hit_tokens": 20, "prompt_tokens_details": {"cached_tokens": 10}}, (10, None, {}, None)),
        ({"input_details": {"cached_tokens": 20, "cached_creation_tokens": 30}}, (20, 30, {}, 30)),
        ({"prompt_tokens_details": {"cached_tokens": 0}, "cached_tokens": 20,
          "input_tokens_details": {"cached_tokens": 20, "cached_creation_tokens": 30}}, (20, 30, {}, 30)),
        ({"cache_write_tokens": 0, "input_details": {"cache_write_tokens_details": {"ttl_12h_tokens": 30}}},
         (None, 30, {"43200": 30}, 0)),
    )
    for usage, expected in cases:
        cursor: Final = await connection.execute(
            f"SELECT {_READ} AS cache_read_tokens,cache_write_tokens,cache_write_ttl_tokens,"
            "cache_write_unclassified_tokens "
            f"FROM (VALUES (%s::jsonb)) s(metadata) {legacy_cache_sql(_WRITE)}",
            (Jsonb({"additional_usage_values": usage}),),
        )
        row: Final = await cursor.fetchone()
        assert row is not None
        assert (row["cache_read_tokens"], row["cache_write_tokens"],
                row["cache_write_ttl_tokens"], row["cache_write_unclassified_tokens"]) == expected

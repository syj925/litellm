import os
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from litellm.proxy.retail.model_metrics import ModelMetricHours, load_model_metrics
from litellm.proxy.retail.store import WalletStore


@pytest.mark.asyncio
async def test_model_ttft_requires_actual_output_observation_and_exact_request_correlation() -> None:
    database_url: Final = os.environ.get("RETAIL_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_model_metrics_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        await _assert_actual_output_metrics(dsn)
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _assert_actual_output_metrics(database_url: str) -> None:
    now: Final = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    start: Final = now.replace(tzinfo=None) - timedelta(minutes=1)
    deployments: Final = ({
        "model_name": "example", "model_info": {"id": "published"}, "litellm_params": {"model": "test/example"},
    },)
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
    store: Final = WalletStore(database_url)
    await store.initialize()
    async with store.transaction() as connection:
        await connection.execute(
            'CREATE TABLE "LiteLLM_SpendLogs"('
            'request_id TEXT PRIMARY KEY,model TEXT,model_group TEXT,model_id TEXT,"user" TEXT,status TEXT,'
            '"startTime" TIMESTAMP(3),"endTime" TIMESTAMP(3),"completionStartTime" TIMESTAMP(3),'
            'request_duration_ms INT,prompt_tokens INT,completion_tokens INT,total_tokens INT,'
            'metadata JSONB,cache_hit TEXT,litellm_call_id TEXT)'
        )
        await connection.execute(
            "INSERT INTO retail_resource_pools(id,name,source_kind,enabled,visibility) "
            "VALUES ('public','Public','anthropic',true,'public')"
        )
        await connection.execute(
            "INSERT INTO retail_pool_bindings(pool_id,deployment_id,model) VALUES ('public','published','example')"
        )
        await connection.execute(
            'INSERT INTO "LiteLLM_SpendLogs" VALUES '
            "('request','example','example','published','account','success',%s,%s,%s,2000,100,32,132,"
            "'{\"observed_stream\":true}','false','call')",
            (start, start + timedelta(seconds=2), start + timedelta(seconds=2)),
        )
    historical: Final = await load_model_metrics(
        store, "example", ModelMetricHours.DAY, None, now, deployments=deployments
    )
    assert historical.summary.requests == 1
    assert historical.summary.ttft_samples == 0
    assert historical.summary.ttft_p50_ms is None
    assert historical.summary.throughput_samples == 0
    assert historical.summary.output_tokens_per_second_p50 is None
    async with store.transaction() as connection:
        await connection.execute(
            "INSERT INTO retail_model_observations VALUES "
            "('call','other-account',%s,%s,%s,'openai_chat','text'),"
            "('call','account',%s,%s,%s,'openai_chat','text')",
            (
                start, start + timedelta(seconds=0.8), start + timedelta(seconds=2.4),
                start + timedelta(milliseconds=1), start + timedelta(seconds=0.8), start + timedelta(seconds=2.4),
            ),
        )
    unrelated: Final = await load_model_metrics(
        store, "example", ModelMetricHours.DAY, None, now, deployments=deployments
    )
    assert unrelated.summary.ttft_samples == 0
    assert unrelated.summary.throughput_samples == 0
    async with store.transaction() as connection:
        await connection.execute(
            'UPDATE "LiteLLM_SpendLogs" SET "completionStartTime"=%s WHERE request_id=\'request\'',
            (start + timedelta(milliseconds=10),),
        )
        await connection.execute(
            "INSERT INTO retail_model_observations VALUES ('call','account',%s,%s,%s,'openai_chat','text')",
            (start, start + timedelta(seconds=0.8), start + timedelta(seconds=2.4)),
        )
    observed: Final = await load_model_metrics(
        store, "example", ModelMetricHours.DAY, None, now, deployments=deployments
    )
    assert observed.summary.requests == 1
    assert observed.summary.ttft_samples == 1
    assert observed.summary.ttft_p50_ms == 800
    assert observed.summary.output_tokens_per_second_p50 == 20
    assert observed.summary.throughput_samples == 1
    assert sum(row.requests for row in observed.trend) == observed.summary.requests

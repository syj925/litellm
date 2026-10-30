import os
from decimal import Decimal
from typing import Final
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from litellm.proxy.retail.models import IdempotencyConflict
from litellm.proxy.retail.pricing import charge_for_usage, cost_breakdown, usage_facts
from litellm.proxy.retail.store import WalletStore


@pytest.mark.asyncio
async def test_cache_prices_migrate_without_repricing_history_and_settle_from_the_frozen_tiers() -> None:
    database_url: Final = os.environ.get("RETAIL_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("An isolated PostgreSQL service with CREATE DATABASE permission is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_cache_prices_" + uuid4().hex
    dsn: Final = make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        await _assert_frozen_cache_prices(dsn)
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def _assert_frozen_cache_prices(database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await connection.execute(
            "CREATE TABLE retail_model_prices(model TEXT PRIMARY KEY,input_cost_per_token NUMERIC(30,18),"
            "output_cost_per_token NUMERIC(30,18),cache_read_input_token_cost NUMERIC(30,18),"
            "cache_creation_input_token_cost NUMERIC(30,18),version BIGINT DEFAULT 1,"
            "updated_at TIMESTAMPTZ DEFAULT now())"
        )
        await connection.execute("INSERT INTO retail_model_prices VALUES ('example',0.000002,0.000010,0.0000002,0.0000025,7,now())")
    store: Final = WalletStore(database_url)
    await store.initialize()
    legacy: Final = await store.get_model_price("example")
    assert legacy is not None
    assert legacy["version"] == 7
    assert legacy["cache_write_tiers"] == {}
    rates: Final = {
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
        "cache_read_input_token_cost": "0.0000002", "cache_creation_input_token_cost": "0.0000025",
        "cache_write_tiers": {"300": "0.0000025", "3600": "0.000004", "750": "0"},
    }
    saved: Final = await store.save_model_price("example", rates)
    assert saved["version"] == 8
    assert saved["cache_write_tiers"] == rates["cache_write_tiers"]
    snapshot: Final = {**saved, "version": "token-v2", "price_version": saved["version"], "discount": "0.65"}
    await store.get_wallet("account", create=True)
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_wallets SET balance_nanos=1000000000 WHERE user_id='account'")
    await store.reserve("account", "request", "example", "0.1", snapshot)
    await store.record_pending_usage("request", {"billing_error": "Fixture awaiting verified usage"})
    pending: Final = await store.get_reservation("request")
    assert pending is not None and pending["status"] == "reserved"
    assert pending["price_snapshot"] == snapshot
    changed: Final = await store.save_model_price("example", {**rates, "cache_write_tiers": {"300": "0.5", "3600": "1"}})
    assert changed["version"] == 9
    facts: Final = usage_facts({"usage": {
        "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600},
    }})
    assert facts is not None
    expected: Final = (Decimal("0.000002") * 100 + Decimal("0.000010") * 10 + Decimal("0.0000002") * 200
                       + Decimal("0.0000025") * 400 + Decimal("0.000004") * 600) * Decimal("0.65")
    amount: Final = charge_for_usage(snapshot, facts)
    assert Decimal(amount) == expected
    details: Final = {"usage": facts.model_dump(mode="json"), "cost_breakdown": cost_breakdown(snapshot, facts)}
    settled: Final = await store.settle("request", amount, details)
    await store.settle("request", amount, details)
    await store.record_pending_usage("request", {"billing_error": "Must not overwrite settled usage"})
    assert await store.get_reservation("request") == settled
    wallet: Final = await store.get_wallet("account")
    assert Decimal(str(wallet["balance"])) == Decimal(1) - expected
    assert wallet["reserved"] == "0.000000000"
    ledger: Final = await store.ledger("account", kind="charge")
    assert ledger["total"] == 1
    with pytest.raises(IdempotencyConflict):
        await store.settle("request", "0", details)

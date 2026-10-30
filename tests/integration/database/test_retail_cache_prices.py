import os
from decimal import Decimal
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
from litellm.proxy.retail.catalog import build_model_catalog
from litellm.proxy.retail.models import IdempotencyConflict, InvalidAmount
from litellm.proxy.retail.pool_store import PoolAccess
from litellm.proxy.retail.pricing import UsageFacts, catalog_snapshot, charge_for_usage, cost_breakdown, usage_facts
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
    assert legacy["cache_write_price_published"] is None
    assert legacy["expression_policy"] is None
    repeated: Final = WalletStore(database_url)
    await repeated.initialize()
    assert await repeated.get_model_price("example") == legacy
    rates: Final = {
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
        "cache_read_input_token_cost": "0.0000002", "cache_creation_input_token_cost": "0.0000025",
        "cache_write_tiers": {"300": "0.0000025", "3600": "0.000004", "750": "0"},
    }
    saved: Final = await store.save_model_price("example", rates)
    assert saved["version"] == 8
    assert saved["cache_write_tiers"] == rates["cache_write_tiers"]
    assert saved["cache_write_price_published"] is True
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
    await _assert_default_duration_prices_are_frozen(store)
    await _assert_catalog_cache_write_provenance(store)
    await _assert_administrator_price_edits(store)
    await _assert_expression_model_prices(store)


async def _assert_default_duration_prices_are_frozen(store: WalletStore) -> None:
    rates: Final = {
        "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000010",
        "cache_read_input_token_cost": "0.0000002", "cache_creation_input_token_cost": "0.0000025",
        "cache_write_tiers": {"300": "0.000004"},
    }
    saved: Final = await store.save_model_price("fallback-write", rates)
    snapshot: Final = {**saved, "version": "token-v3", "price_version": saved["version"], "discount": "0.65"}
    await store.get_wallet("fallback-account", create=True)
    async with store.transaction() as connection:
        await connection.execute("UPDATE retail_wallets SET balance_nanos=1000000000 WHERE user_id='fallback-account'")
    await store.reserve("fallback-account", "fallback-request", "fallback-write", "0.1", snapshot)
    await store.save_model_price("fallback-write", {
        **rates, "cache_creation_input_token_cost": "0.5", "cache_write_tiers": {"300": "1", "43200": "2"},
    })
    facts: Final = UsageFacts(
        input_tokens=100, output_tokens=10, cache_read_tokens=200, cache_write_tokens=1000,
        cache_write_ttl_tokens={"300": 400, "43200": 600},
    )
    expected: Final = (
        Decimal("0.000002") * 100 + Decimal("0.000010") * 10 + Decimal("0.0000002") * 200
        + Decimal("0.000004") * 400 + Decimal("0.0000025") * 600
    ) * Decimal("0.65")
    amount: Final = charge_for_usage(snapshot, facts)
    assert Decimal(amount) == expected
    details: Final = {"usage": facts.model_dump(mode="json"), "cost_breakdown": cost_breakdown(snapshot, facts)}
    await store.settle("fallback-request", amount, details)
    await store.settle("fallback-request", amount, details)
    reservation: Final = await store.get_reservation("fallback-request")
    assert reservation is not None and reservation["status"] == "settled"
    assert reservation["price_snapshot"] == snapshot
    assert reservation["details"]["usage"]["cache_write_ttl_tokens"] == {"300": 400, "43200": 600}
    assert Decimal(reservation["details"]["cost_breakdown"]["cache_write_tiers"]["43200"]) == (
        Decimal("0.0000025") * 600 * Decimal("0.65")
    )
    wallet: Final = await store.get_wallet("fallback-account")
    assert Decimal(str(wallet["balance"])) == Decimal(1) - expected
    assert wallet["reserved"] == "0.000000000"
    assert (await store.ledger("fallback-account", kind="charge"))["total"] == 1


async def _assert_catalog_cache_write_provenance(store: WalletStore) -> None:
    catalog: Final = {"gemini/catalog-fixture": {
        "mode": "chat", "litellm_provider": "gemini", "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000006", "cache_read_input_token_cost": "0.0000002",
    }}
    for operation in ("auto", "import"):
        model: Final = "publication-" + operation
        deployment: Final = {
            "model_name": model, "model_info": {"id": model},
            "litellm_params": {"model": "gemini/catalog-fixture"},
        }
        initial: Final = catalog_snapshot(model, (deployment,), "1", catalog)
        assert initial["cache_write_price_published"] is False
        assert Decimal(str(initial["cache_creation_input_token_cost"])) == Decimal("0.000002")
        if operation == "auto":
            await store.get_model_price(model, initial)
        else:
            await store.save_reference_prices((initial,))
        saved: Final = await store.get_model_price(model)
        assert saved is not None and saved["cache_write_price_published"] is False
        assert saved["version"] == 1
        assert Decimal(str(saved["cache_creation_input_token_cost"])) == Decimal("0.000002")
        page: Final = build_model_catalog(
            (), (), PoolAccess(), (deployment,), (saved,), (), catalog, mode="public",
        )
        pricing: Final = page.items[0].reference_pricing
        assert pricing is not None and pricing.cache_write is None
        assert await store.get_model_price(model) == saved
    for amount, published in (("0.000002", False), ("0", True), ("0.000003", None)):
        manual_model: Final = "manual-" + amount
        manual_saved: Final = await store.save_model_price(manual_model, {
            "input_cost_per_token": "0.000002", "output_cost_per_token": "0.000006",
            "cache_read_input_token_cost": "0.0000002", "cache_creation_input_token_cost": amount,
            "cache_write_price_published": published,
        })
        assert manual_saved["cache_write_price_published"] is (True if published is None else published)
        manual_page: Final = build_model_catalog(
            (), (), PoolAccess(), ({"model_name": manual_model, "model_info": {"id": manual_model},
                                   "litellm_params": {"model": "gemini/catalog-fixture"}},),
            (manual_saved,), (), catalog, mode="public",
        )
        manual_pricing: Final = manual_page.items[0].reference_pricing
        assert manual_pricing is not None
        displayed: Final = Decimal(manual_pricing.cache_write) if manual_pricing.cache_write is not None else None
        assert displayed == (None if published is False else Decimal(amount) * 1_000_000)


async def _assert_administrator_price_edits(store: WalletStore) -> None:
    async def isolated_store() -> WalletStore:
        return store

    async def administrator() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="fixture-admin", user_role=LitellmUserRoles.PROXY_ADMIN)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[retail_store] = isolated_store
    app.dependency_overrides[user_api_key_auth] = administrator
    payload: Final = {
        "model": "api-public-model", "input_per_million": "2", "output_per_million": "6",
        "cache_read_per_million": "0.2", "cache_write_per_million": "2",
        "cache_write_tiers": {"75": "0"}, "cache_write_price_published": False,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        preserved: Final = await client.post("/retail/admin/model-prices", json=payload)
        assert preserved.status_code == 200, preserved.text
        assert preserved.json()["cache_write_price_published"] is False
        assert Decimal(preserved.json()["output_per_million"]) == Decimal(payload["output_per_million"])
        listing: Final = await client.get("/retail/admin/pricing-catalog")
        assert listing.status_code == 200, listing.text
        row: Final = next(item for item in listing.json()["items"] if item["model"] == payload["model"])
        assert row["status"] == "custom"
        assert row["price"]["cache_write_price_published"] is False
        assert row["price"]["cache_write_tiers"] == payload["cache_write_tiers"]
        published: Final = await client.post("/retail/admin/model-prices", json={
            **payload, "cache_write_per_million": "0", "cache_write_price_published": True,
        })
        assert published.status_code == 200, published.text
        assert published.json()["cache_write_price_published"] is True
        assert Decimal(published.json()["cache_write_per_million"]) == 0
        assert published.json()["version"] == preserved.json()["version"] + 1


async def _assert_expression_model_prices(store: WalletStore) -> None:
    async def isolated_store() -> WalletStore:
        return store

    async def administrator() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="fixture-admin", user_role=LitellmUserRoles.PROXY_ADMIN)

    policy: Final = {
        "version": "expression-v1", "expression": "p * 2 + c * 6", "timezone": "UTC", "usage_bounds": {},
    }
    payload: Final = {
        "model": "expression-api-model", "input_per_million": "9", "output_per_million": "9",
        "cache_read_per_million": "9", "cache_write_per_million": "9", "expression_policy": policy,
    }
    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[retail_store] = isolated_store
    app.dependency_overrides[user_api_key_auth] = administrator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        saved: Final = await client.post("/retail/admin/model-prices", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["expression_policy"] == policy
        original: Final = await store.get_model_price(payload["model"])
        assert original is not None and original["expression_policy"] == policy
        await store.reserve("account", "expression-frozen-request", payload["model"], "0.01", original)
        listing: Final = await client.get("/retail/admin/pricing-catalog")
        assert listing.status_code == 200, listing.text
        row: Final = next(item for item in listing.json()["items"] if item["model"] == payload["model"])
        assert row["status"] == "expression" and row["price"]["expression_policy"] == policy
        rejected: Final = await client.post("/retail/admin/model-prices", json={
            **payload, "expression_policy": {**policy, "expression": "__import__('os')"},
        })
        assert rejected.status_code == 422, rejected.text
        assert await store.get_model_price(payload["model"]) == original
        cleared: Final = await client.post("/retail/admin/model-prices", json={
            key: value for key, value in payload.items() if key != "expression_policy"
        })
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["expression_policy"] is None
        assert cleared.json()["version"] == saved.json()["version"] + 1
        restored: Final = await client.post("/retail/admin/model-prices", json=payload)
        explicit_null: Final = await client.post(
            "/retail/admin/model-prices", json={**payload, "expression_policy": None},
        )
        assert explicit_null.status_code == 200, explicit_null.text
        assert explicit_null.json()["expression_policy"] is None
        assert explicit_null.json()["version"] == restored.json()["version"] + 1
        await client.post("/retail/admin/model-prices", json=payload)
    imported: Final = await store.save_reference_prices(({
        **original, "source": "catalog", "catalog_model": "fixture", "source_label": "Imported catalog basis",
        "expression_policy": None,
    },))
    assert imported["items"][0]["expression_policy"] is None
    assert imported["items"][0]["source"] == "catalog"
    imported_policy: Final = {**policy, "expression": "p * 3 + c * 9"}
    dynamic_import: Final = await store.save_reference_prices(({
        **original, "source": "catalog", "catalog_model": "fixture", "source_label": "Imported expression basis",
        "expression_policy": imported_policy,
    },))
    assert dynamic_import["items"][0]["expression_policy"] == imported_policy
    assert dynamic_import["items"][0]["version"] == imported["items"][0]["version"] + 1
    with pytest.raises(InvalidAmount):
        await store.save_model_price(payload["model"], {**original, "expression_policy": {"expression": "secret"}})
    assert await store.get_model_price(payload["model"]) == dynamic_import["items"][0]
    frozen: Final = await store.get_reservation("expression-frozen-request")
    assert frozen is not None and frozen["price_snapshot"] == original
    assert frozen["price_snapshot"]["expression_policy"] == policy
    await store.release("expression-frozen-request", "Expression snapshot verified")

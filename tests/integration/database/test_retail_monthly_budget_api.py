import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import JsonValue

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail import api, monthly_budgets
from litellm.proxy.retail.monthly_budget_store import following_month
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class NativeWriter:
    url: str

    async def query_raw(self, query: str, *args: object) -> object:
        async with await psycopg.AsyncConnection.connect(self.url, row_factory=dict_row) as connection:
            return await (await connection.execute(query.replace("$1", "%s"), args)).fetchall()


@dataclass(frozen=True, slots=True)
class NativeClient:
    writer_db: NativeWriter


@dataclass(frozen=True, slots=True)
class ModelDirectory:
    model_names: tuple[str, ...] = ("public-model",)


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    client: httpx.AsyncClient


def create_payload() -> Mapping[str, str]:
    later: Final = following_month(following_month(datetime.now(timezone.utc), "UTC"), "UTC")
    return {
        "model": "public-model",
        "start_month": later.strftime("%Y-%m"),
        "timezone": "UTC",
        "limit_usd": "0.000000009",
        "currency": "USD",
    }


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    address: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not address:
        pytest.skip("A dedicated monthly API PostgreSQL service is required")
    base: Final = conninfo_to_dict(address)
    name: Final = "retail_monthly_api_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url: Final = make_conninfo(**{**base, "dbname": name})
    wallet: Final = WalletStore(url, max_connections=2, diagnostic_connections=2, critical_connections=1)
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MONTHLY_BUDGETS_ENABLED", "true")

    async def authentication(request: Request) -> UserAPIKeyAuth:
        token: Final = request.headers.get("Authorization", "").removeprefix("Bearer ")
        owner: Final = "account" if token == "fixture-account" else "admin"
        role: Final = LitellmUserRoles.INTERNAL_USER if owner == "account" else LitellmUserRoles.PROXY_ADMIN
        return UserAPIKeyAuth(user_id=owner, api_key=token, user_role=role, team_id=UI_SESSION_TOKEN_TEAM_ID)

    async def storage() -> WalletStore:
        return wallet

    app: Final = FastAPI()
    app.include_router(monthly_budgets.router)
    app.dependency_overrides[api.user_api_key_auth] = authentication
    app.dependency_overrides[api.retail_store] = storage
    try:
        async with await psycopg.AsyncConnection.connect(url) as connection:
            await connection.execute("""CREATE TABLE "LiteLLM_UserTable"(
                user_id TEXT PRIMARY KEY,user_role TEXT,password_reset_required BOOLEAN DEFAULT false)""")
            await connection.execute("""CREATE TABLE "LiteLLM_VerificationToken"(
                token TEXT PRIMARY KEY,user_id TEXT,team_id TEXT,blocked BOOLEAN DEFAULT false,
                expires TIMESTAMPTZ,allowed_routes TEXT[] DEFAULT '{}')""")
            await connection.execute("""INSERT INTO "LiteLLM_UserTable"(user_id,user_role) VALUES
                ('admin','proxy_admin'),('account','internal_user'),('other-account','internal_user')""")
            await connection.execute(
                """INSERT INTO "LiteLLM_VerificationToken"(token,user_id,team_id) VALUES
                ('fixture-admin','admin',%s),('fixture-account','account',%s)""",
                (UI_SESSION_TOKEN_TEAM_ID, UI_SESSION_TOKEN_TEAM_ID),
            )
        from litellm.proxy import proxy_server

        monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(NativeWriter(url)))
        monkeypatch.setattr(proxy_server, "llm_router", ModelDirectory())
        await wallet.initialize()
        await wallet.initialize_control_plane()
        for owner in ("account", "other-account"):
            await wallet.get_wallet(owner, create=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer fixture-admin"},
        ) as client:
            yield Runtime(wallet, client)
    finally:
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def create(runtime: Runtime, owner: str = "account") -> Mapping[str, JsonValue]:
    response: Final = await runtime.client.post(
        f"/retail/admin/users/{owner}/monthly-budgets",
        json=create_payload(),
        headers={"Idempotency-Key": "fixture-create-" + owner},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_admin_create_get_update_stop_use_exact_terms_versions_and_private_responses(runtime: Runtime) -> None:
    created: Final = await create(runtime)
    base: Final = f"/retail/admin/users/account/monthly-budgets/{created['id']}"
    read: Final = await runtime.client.get(base)
    assert read.status_code == 200 and read.json() == created
    assert read.headers["Cache-Control"] == "private, no-store"
    assert read.headers["Vary"] == "Authorization, Cookie"
    update: Final = await runtime.client.patch(
        base, json={"limit_usd": "1.000000001", "expected_version": 1}, headers={"Idempotency-Key": "fixture-update"}
    )
    assert update.status_code == 200 and update.json()["version"] == 2
    assert update.json()["limit_usd"] == "1.000000001"
    stale: Final = await runtime.client.patch(
        base, json={"limit_usd": "2", "expected_version": 1}, headers={"Idempotency-Key": "fixture-stale"}
    )
    assert stale.status_code == 409
    stop: Final = await runtime.client.post(
        base + "/stop", json={"expected_version": 2}, headers={"Idempotency-Key": "fixture-stop"}
    )
    assert stop.status_code == 200 and stop.json()["state"] == "stopped"
    assert stop.json()["stops_at"] == stop.json()["starts_at"]
    assert (
        await runtime.client.post(
            base + "/stop", json={"expected_version": 2}, headers={"Idempotency-Key": "fixture-stop"}
        )
    ).json() == stop.json()


@pytest.mark.asyncio
async def test_own_list_and_detail_project_only_owner_and_reject_scope_injection(runtime: Runtime) -> None:
    own: Final = await create(runtime)
    foreign: Final = await create(runtime, "other-account")
    headers: Final = {"Authorization": "Bearer fixture-account"}
    listed: Final = await runtime.client.get("/retail/monthly-budgets", headers=headers)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    expected: Final = {
        key: value
        for key, value in own.items()
        if key not in {"user_id", "created_at", "created_by", "updated_at", "updated_by"}
    }
    assert listed.json()["items"] == [expected]
    assert (await runtime.client.get(f"/retail/monthly-budgets/{own['id']}", headers=headers)).json() == expected
    denied: Final = await runtime.client.get(f"/retail/monthly-budgets/{foreign['id']}", headers=headers)
    assert denied.status_code == 404 and foreign["id"] not in denied.text
    injected: Final = await runtime.client.get("/retail/monthly-budgets?user_id=other-account", headers=headers)
    assert injected.status_code == 422 and "other-account" not in injected.text
    assert injected.headers["Cache-Control"] == "private, no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    (
        """UPDATE "LiteLLM_UserTable" SET user_role='internal_user' WHERE user_id='admin' """,
        """UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin_viewer' WHERE user_id='admin' """,
        """UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE user_id='admin' """,
        """UPDATE "LiteLLM_VerificationToken" SET expires=clock_timestamp()-interval '1 second' WHERE user_id='admin' """,
    ),
)
async def test_cached_administrator_cannot_bypass_fresh_authority(runtime: Runtime, statement: str) -> None:
    created: Final = await create(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute(statement)
    response: Final = await runtime.client.patch(
        f"/retail/admin/users/account/monthly-budgets/{created['id']}",
        json={"limit_usd": "1", "expected_version": 1},
        headers={"Idempotency-Key": "fixture-revoked"},
    )
    assert response.status_code == 403 and response.headers["Cache-Control"] == "private, no-store"
    async with runtime.wallet.diagnostic_transaction() as connection:
        assert (await (await connection.execute("SELECT version FROM retail_model_budget_recurrences")).fetchone())[
            "version"
        ] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ("/retail/monthly-budgets?actor_id=secret-fixture", "/retail/admin/users/account/monthly-budgets?currency=CNY"),
)
async def test_lists_reject_unknown_query_fields_without_echo(runtime: Runtime, path: str) -> None:
    response: Final = await runtime.client.get(path)
    assert response.status_code == 422 and "secret-fixture" not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.asyncio
async def test_write_unknown_fields_queries_and_disabled_flag_fail_without_saving(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown: Final = await runtime.client.post(
        "/retail/admin/users/account/monthly-budgets",
        json={**create_payload(), "api_key": "secret-fixture"},
        headers={"Idempotency-Key": "fixture-unknown"},
    )
    assert unknown.status_code == 422 and "secret-fixture" not in unknown.text
    query: Final = await runtime.client.post(
        "/retail/admin/users/account/monthly-budgets?q=secret-fixture",
        json=create_payload(),
        headers={"Idempotency-Key": "fixture-query"},
    )
    assert query.status_code == 422 and "secret-fixture" not in query.text
    monkeypatch.setenv("RETAIL_MONTHLY_BUDGETS_ENABLED", "false")
    disabled: Final = await runtime.client.get("/retail/monthly-budgets")
    assert disabled.status_code == 404 and disabled.headers["Cache-Control"] == "private, no-store"
    async with runtime.wallet.diagnostic_transaction() as connection:
        assert (
            await (await connection.execute("SELECT count(*) AS total FROM retail_model_budget_recurrences")).fetchone()
        )["total"] == 0

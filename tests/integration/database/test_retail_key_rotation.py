import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.retail import api, key_rotation
from litellm.proxy.retail.control_plane_access import PrismaIdentityReader
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class NativeWriter:
    database_url: str

    async def query_raw(self, query: str, *args: object) -> object:
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
            return await (await connection.execute(query.replace("$1", "%s"), args)).fetchall()


@dataclass(frozen=True, slots=True)
class NativeClient:
    writer_db: NativeWriter


@dataclass(frozen=True, slots=True)
class NativeTransaction:
    connection: psycopg.AsyncConnection

    async def query_raw(self, query: str, *args: object) -> object:
        return await (await self.connection.execute(query.replace("$1", "%s"), args)).fetchall()


@dataclass(frozen=True, slots=True)
class KeyRuntime:
    wallet: WalletStore
    native_url: str
    client: httpx.AsyncClient
    cached: UserAPIKeyAuth
    reader: PrismaIdentityReader


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[KeyRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated key-rotation-test PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    names: Final = ("retail_key_" + uuid4().hex, "retail_key_native_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    retail_url: Final = make_conninfo(**{**base, "dbname": names[0]})
    native_url: Final = make_conninfo(**{**base, "dbname": names[1]})
    wallet: Final = WalletStore(
        retail_url,
        max_connections=1,
        diagnostic_connections=1,
        critical_connections=1,
        borrow_timeout=0.15,
        lock_timeout_ms=100,
    )
    cached: Final = UserAPIKeyAuth(user_id="owner", api_key="fixture-owner", user_role=LitellmUserRoles.PROXY_ADMIN)
    writer: Final = NativeWriter(native_url)

    async def auth() -> UserAPIKeyAuth:
        return cached

    async def storage() -> WalletStore:
        return wallet

    app: Final = FastAPI()
    app.include_router(key_rotation.router)
    app.dependency_overrides[api.retail_user] = auth
    app.dependency_overrides[api.retail_store] = storage
    try:
        async with await psycopg.AsyncConnection.connect(retail_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with await psycopg.AsyncConnection.connect(native_url) as connection:
            await connection.execute("""CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY,user_role TEXT,
                password_reset_required BOOLEAN DEFAULT false)""")
            await connection.execute("""CREATE TABLE "LiteLLM_VerificationToken"(token TEXT PRIMARY KEY,user_id TEXT,team_id TEXT,
                blocked BOOLEAN DEFAULT false,expires TIMESTAMPTZ,allowed_routes TEXT[] DEFAULT '{}')""")
            await connection.execute(
                "INSERT INTO \"LiteLLM_UserTable\"(user_id,user_role) VALUES('owner','internal_user')"
            )
            await connection.execute(
                "INSERT INTO \"LiteLLM_VerificationToken\"(token,user_id) VALUES('fixture-owner','owner')"
            )
        from litellm.proxy import proxy_server

        monkeypatch.setattr(proxy_server, "prisma_client", NativeClient(writer))
        monkeypatch.setattr(proxy_server, "master_key", "fixture-master")
        await wallet.initialize()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield KeyRuntime(wallet, native_url, client, cached, PrismaIdentityReader(writer))
    finally:
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.asyncio
async def test_rotation_demotion_and_credential_revocation_use_live_separate_native_database(
    runtime: KeyRuntime,
) -> None:
    current: Final = await key_rotation.rotation_user(runtime.cached, "fixture-master", reader=runtime.reader)
    assert current.user_role == LitellmUserRoles.INTERNAL_USER
    assert runtime.cached.user_role == LitellmUserRoles.PROXY_ADMIN
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('UPDATE "LiteLLM_VerificationToken" SET blocked=true')
    result: Final = await runtime.client.post("/retail/keys/rotate", json={"key": "fixture-target"})
    assert result.status_code == 403 and "no-store" in result.headers["Cache-Control"]
    assert result.headers["Vary"] == "Authorization, Cookie"
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as connection:
        await connection.execute('UPDATE "LiteLLM_VerificationToken" SET blocked=false')
        await connection.execute('UPDATE "LiteLLM_UserTable" SET password_reset_required=true')
    with pytest.raises(HTTPException) as denied:
        await key_rotation.rotation_user(runtime.cached, "fixture-master", reader=runtime.reader)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_rotation_lock_pool_saturation_returns_private_safe_503(runtime: KeyRuntime) -> None:
    async with runtime.wallet.diagnostic_transaction():
        result: Final = await runtime.client.post("/retail/keys/rotate", json={"key": "fixture-target"})
    assert result.status_code == 503
    assert result.json() == {"detail": "Key storage is temporarily unavailable"}
    assert "no-store" in result.headers["Cache-Control"]
    assert runtime.wallet.pool_stats()["diagnostic"]["pool_size"] <= 1


@pytest.mark.asyncio
async def test_rotation_contended_advisory_lock_has_timeout_and_does_not_touch_native_key(runtime: KeyRuntime) -> None:
    target: Final = "fixture-target"
    hashed: Final = key_rotation.rotation_target(target, "fixture-master", runtime.cached)
    async with await psycopg.AsyncConnection.connect(runtime.wallet.database_url) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:rotate:" + hashed,))
        result: Final = await runtime.client.post("/retail/keys/rotate", json={"key": target})
    assert result.status_code == 503 and result.json() == {"detail": "Key storage is temporarily unavailable"}
    credential: Final = await runtime.reader.credential("fixture-owner")
    assert credential is not None and credential.blocked is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    (
        """UPDATE "LiteLLM_UserTable" SET user_role='proxy_admin_viewer' WHERE user_id='owner' """,
        """UPDATE "LiteLLM_VerificationToken" SET blocked=true WHERE token='fixture-owner' """,
        """UPDATE "LiteLLM_VerificationToken" SET user_id='other-owner' WHERE token='fixture-target' """,
    ),
)
async def test_rotation_writer_locks_fence_actor_revocation_and_target_ownership_transfer(
    runtime: KeyRuntime, statement: str
) -> None:
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as initialize:
        await initialize.execute(
            """INSERT INTO "LiteLLM_VerificationToken"(token,user_id) VALUES('fixture-target','owner')"""
        )
    async with await psycopg.AsyncConnection.connect(runtime.native_url, row_factory=dict_row) as transaction:
        await key_rotation.lock_rotation_rows(NativeTransaction(transaction), runtime.cached, "fixture-target")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            async with await psycopg.AsyncConnection.connect(runtime.native_url) as concurrent:
                await concurrent.execute("SET LOCAL lock_timeout='100ms'")
                await concurrent.execute(statement)
    async with await psycopg.AsyncConnection.connect(runtime.native_url) as concurrent:
        changed: Final = await concurrent.execute(statement)
        assert changed.rowcount == 1

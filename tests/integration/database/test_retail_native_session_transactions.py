import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.login_utils import PASSWORD_SESSION_METADATA
from litellm.proxy.retail.native_audit_store import NativeAuditUnavailable, ensure_native_schema
from litellm.proxy.retail.native_session_transactions import (
    NativeSessionFailure,
    NativeSessionSuccess,
    change_password_in_transaction,
    logout_in_transaction,
)


@dataclass(frozen=True, slots=True)
class NativeWriter:
    connection: psycopg.AsyncConnection

    async def query_raw(self, query: str, *args: object) -> object:
        positions: Final = tuple(int(match[1]) - 1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(re.sub(r"\$\d+", "%s", query), tuple(args[i] for i in positions))
        return await cursor.fetchall()

    async def execute_raw(self, query: str, *args: object) -> int:
        positions: Final = tuple(int(match[1]) - 1 for match in re.finditer(r"\$(\d+)", query))
        cursor: Final = await self.connection.execute(re.sub(r"\$\d+", "%s", query), tuple(args[i] for i in positions))
        return cursor.rowcount


@dataclass(frozen=True, slots=True)
class NativeClient:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta = timedelta(seconds=8)) -> AsyncIterator[NativeWriter]:
        async with asyncio.timeout(timeout.total_seconds()):
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                await connection.execute("SET LOCAL lock_timeout='1000ms'")
                yield NativeWriter(connection)


@dataclass(frozen=True, slots=True)
class NativeFixture:
    client: NativeClient
    auth: UserAPIKeyAuth
    old_hash: str


@pytest_asyncio.fixture(loop_scope="function")
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[NativeFixture]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated native-session PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_native_sessions_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url: Final = make_conninfo(**{**base, "dbname": name})
    client: Final = NativeClient(url)
    auth: Final = UserAPIKeyAuth(
        user_id="fixture-owner",
        token="fixture-caller-token",
        team_id=UI_SESSION_TOKEN_TEAM_ID,
        user_role=LitellmUserRoles.INTERNAL_USER,
        metadata=dict(PASSWORD_SESSION_METADATA),
    )
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    try:
        async with client.tx() as writer:
            await writer.execute_raw("""CREATE TABLE "LiteLLM_UserTable"(
                user_id TEXT PRIMARY KEY,user_role TEXT,password TEXT,password_reset_required BOOLEAN,
                last_breach_check_at TIMESTAMPTZ,updated_at TIMESTAMPTZ)""")
            await writer.execute_raw("""CREATE TABLE "LiteLLM_VerificationToken"(
                token TEXT PRIMARY KEY,user_id TEXT,team_id TEXT,metadata JSONB DEFAULT '{}',
                allowed_routes TEXT[] DEFAULT '{}',blocked BOOLEAN DEFAULT false,
                expires TIMESTAMPTZ,organization_id TEXT)""")
            await writer.execute_raw("""INSERT INTO "LiteLLM_UserTable" VALUES
                ('fixture-owner','internal_user','fixture-old-password-hash',true,clock_timestamp(),clock_timestamp())""")
            for token, owner, team in (
                ("fixture-caller-token", "fixture-owner", UI_SESSION_TOKEN_TEAM_ID),
                ("fixture-other-password-token", "fixture-owner", UI_SESSION_TOKEN_TEAM_ID),
                ("fixture-other-sso-token", "fixture-owner", UI_SESSION_TOKEN_TEAM_ID),
                ("fixture-api-token", "fixture-owner", "ordinary-team"),
                ("fixture-foreign-token", "other-owner", UI_SESSION_TOKEN_TEAM_ID),
            ):
                await writer.execute_raw(
                    """INSERT INTO "LiteLLM_VerificationToken"(token,user_id,team_id,metadata)
                    VALUES($1,$2,$3,$4::jsonb)""",
                    token,
                    owner,
                    team,
                    json.dumps(dict(PASSWORD_SESSION_METADATA)),
                )
        await ensure_native_schema(client)
        yield NativeFixture(client, auth, "fixture-old-password-hash")
    finally:
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def state(native: NativeFixture) -> object:
    async with native.client.tx() as writer:
        return (
            await writer.query_raw(
                'SELECT password,password_reset_required,last_breach_check_at FROM "LiteLLM_UserTable"'
            ),
            await writer.query_raw('SELECT token FROM "LiteLLM_VerificationToken" ORDER BY token'),
            await writer.query_raw("SELECT * FROM retail_native_audit_outbox ORDER BY action"),
        )


@pytest.mark.asyncio
async def test_forced_reset_can_change_own_password_and_only_other_ui_sessions_are_revoked_atomically(
    native: NativeFixture,
) -> None:
    async with native.client.tx() as writer:
        outcome: Final = await change_password_in_transaction(
            writer,
            native.auth,
            expected_hash=native.old_hash,
            replacement_hash="fixture-new-password-hash",
        )
    assert isinstance(outcome, NativeSessionSuccess) and outcome.changed
    assert set(outcome.revoked_tokens()) == {"fixture-other-password-token", "fixture-other-sso-token"}
    async with native.client.tx() as writer:
        assert await writer.query_raw(
            'SELECT password,password_reset_required,last_breach_check_at FROM "LiteLLM_UserTable"'
        ) == [
            {
                "password": "fixture-new-password-hash",
                "password_reset_required": False,
                "last_breach_check_at": None,
            }
        ]
        assert await writer.query_raw('SELECT token FROM "LiteLLM_VerificationToken" ORDER BY token') == [
            {"token": "fixture-api-token"},
            {"token": "fixture-caller-token"},
            {"token": "fixture-foreign-token"},
        ]
        events: Final = await writer.query_raw(
            "SELECT action,affected_count FROM retail_native_audit_outbox ORDER BY action"
        )
        assert events == [
            {"action": "native_password.changed", "affected_count": 1},
            {"action": "native_session.revoked", "affected_count": 2},
        ]
        payload: Final = json.dumps(await writer.query_raw("SELECT * FROM retail_native_audit_outbox"), default=str)
    for secret in (
        native.old_hash,
        "fixture-new-password-hash",
        "fixture-caller-token",
        "fixture-other-password-token",
    ):
        assert secret not in payload


@pytest.mark.asyncio
async def test_two_concurrent_password_changes_using_one_old_hash_have_one_winner(native: NativeFixture) -> None:
    async def change(replacement: str) -> object:
        async with native.client.tx() as writer:
            return await change_password_in_transaction(
                writer,
                native.auth,
                expected_hash=native.old_hash,
                replacement_hash=replacement,
            )

    outcomes: Final = await asyncio.gather(change("fixture-winner-a"), change("fixture-winner-b"))
    assert sum(isinstance(outcome, NativeSessionSuccess) for outcome in outcomes) == 1
    assert tuple(outcome.reason for outcome in outcomes if isinstance(outcome, NativeSessionFailure)) == (
        "password_changed",
    )
    async with native.client.tx() as writer:
        assert await writer.query_raw(
            "SELECT action,count(*) AS total FROM retail_native_audit_outbox GROUP BY action ORDER BY action"
        ) == [
            {"action": "native_password.changed", "total": 1},
            {"action": "native_session.revoked", "total": 1},
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    (
        "DELETE FROM \"LiteLLM_VerificationToken\" WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_VerificationToken\" SET expires=clock_timestamp()-interval '1 second' WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_VerificationToken\" SET user_id='other-owner' WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_VerificationToken\" SET team_id='ordinary-team' WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_VerificationToken\" SET metadata='{}' WHERE token='fixture-caller-token'",
        "UPDATE \"LiteLLM_UserTable\" SET user_role='disabled-role' WHERE user_id='fixture-owner'",
    ),
)
async def test_password_write_rechecks_fresh_authority_after_password_was_verified(
    native: NativeFixture,
    statement: str,
) -> None:
    async with native.client.tx() as writer:
        await writer.execute_raw(statement)
    before: Final = await state(native)
    async with native.client.tx() as writer:
        outcome: Final = await change_password_in_transaction(
            writer,
            native.auth,
            expected_hash=native.old_hash,
            replacement_hash="fixture-must-not-write",
        )
    assert outcome == NativeSessionFailure("inactive")
    assert await state(native) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("logout", (False, True))
async def test_full_native_outbox_rolls_back_password_and_session_deletions(
    native: NativeFixture, logout: bool
) -> None:
    async with native.client.tx() as writer:
        await writer.execute_raw("""INSERT INTO retail_native_audit_settings(singleton,source_id,retained_count)
            VALUES(true,gen_random_uuid(),10000)""")
    before: Final = await state(native)
    with pytest.raises(NativeAuditUnavailable):
        async with native.client.tx() as writer:
            if logout:
                await logout_in_transaction(writer, native.auth)
            else:
                await change_password_in_transaction(
                    writer,
                    native.auth,
                    expected_hash=native.old_hash,
                    replacement_hash="fixture-must-roll-back",
                )
    assert await state(native) == before


@pytest.mark.asyncio
async def test_logout_is_self_only_idempotent_and_cannot_delete_a_foreign_session(native: NativeFixture) -> None:
    async with native.client.tx() as writer:
        foreign: Final = await logout_in_transaction(
            writer, native.auth.model_copy(update={"token": "fixture-foreign-token"})
        )
    assert foreign == NativeSessionFailure("inactive")
    async with native.client.tx() as writer:
        first: Final = await logout_in_transaction(writer, native.auth)
    assert isinstance(first, NativeSessionSuccess) and first.revoked_tokens() == ("fixture-caller-token",)
    async with native.client.tx() as writer:
        assert await logout_in_transaction(writer, native.auth) == NativeSessionSuccess(False, ())
        assert await writer.query_raw("SELECT action,affected_count FROM retail_native_audit_outbox") == [
            {"action": "native_session.revoked", "affected_count": 1},
        ]
        assert await writer.query_raw('SELECT count(*) AS total FROM "LiteLLM_VerificationToken"') == [{"total": 4}]


@pytest.mark.asyncio
async def test_password_transaction_locks_fence_concurrent_user_and_credential_revocation(
    native: NativeFixture,
) -> None:
    async with native.client.tx() as writer:
        outcome: Final = await change_password_in_transaction(
            writer,
            native.auth,
            expected_hash=native.old_hash,
            replacement_hash="fixture-locked-change",
        )
        assert isinstance(outcome, NativeSessionSuccess)
        for statement in (
            "UPDATE \"LiteLLM_UserTable\" SET user_role='disabled-role' WHERE user_id='fixture-owner'",
            "UPDATE \"LiteLLM_VerificationToken\" SET blocked=true WHERE token='fixture-caller-token'",
        ):
            with pytest.raises(psycopg.errors.LockNotAvailable):
                async with native.client.tx() as concurrent:
                    await concurrent.execute_raw("SET LOCAL lock_timeout='100ms'")
                    await concurrent.execute_raw(statement)

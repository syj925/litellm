import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from litellm.proxy.retail.native_audit_store import (
    NativeAuditAction,
    NativeAuditClaim,
    NativeAuditConflict,
    NativeAuditStore,
    NativeAuditUnavailable,
    append_native_audit,
    ensure_native_schema,
)
from litellm.proxy.retail.native_audit_worker import NativeAuditWorker
from litellm.proxy.retail.operation_events import append_operation_event
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class NativeTransaction:
    connection: psycopg.AsyncConnection

    async def query_raw(self, query: str, *args: object) -> object:
        positions: Final = tuple(int(value) - 1 for value in re.findall(r"\$(\d+)", query))
        statement: Final = re.sub(r"\$\d+", "%s", query)
        return await (await self.connection.execute(statement, tuple(args[index] for index in positions))).fetchall()

    async def execute_raw(self, query: str, *args: object) -> int:
        return (await self.connection.execute(query, args or None, prepare=False)).rowcount


@dataclass(frozen=True, slots=True)
class NativeClient:
    database_url: str

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[NativeTransaction]:
        async with asyncio.timeout(timeout.total_seconds()), await psycopg.AsyncConnection.connect(
            self.database_url, row_factory=dict_row,
        ) as connection:
            yield NativeTransaction(connection)


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    native: NativeClient
    store: NativeAuditStore
    peer: NativeAuditStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    address: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not address:
        pytest.skip("A dedicated native audit PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    base: Final = conninfo_to_dict(address)
    names: Final = ("native_audit_retail_" + uuid4().hex, "native_audit_source_" + uuid4().hex)
    with psycopg.connect(**base, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    retail_url: Final = make_conninfo(**{**base, "dbname": names[0]})
    native: Final = NativeClient(make_conninfo(**{**base, "dbname": names[1]}))
    wallet: Final = WalletStore(retail_url, max_connections=1, critical_connections=1, diagnostic_connections=1)
    store: Final = NativeAuditStore(native, retail_url)
    peer: Final = NativeAuditStore(native, retail_url)
    try:
        async with await psycopg.AsyncConnection.connect(retail_url) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        async with native.tx(timeout=timedelta(seconds=5)) as writer:
            await writer.execute_raw("CREATE TABLE native_business(id INTEGER PRIMARY KEY,credential TEXT)")
            await writer.query_raw("INSERT INTO native_business VALUES(1,'fixture-original-private-key') RETURNING id")
        await wallet.initialize()
        await wallet.initialize_control_plane()
        await store.initialize()
        await peer.initialize()
        yield Runtime(wallet, native, store, peer)
    finally:
        await asyncio.gather(store.close(), peer.close())
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def native_change(runtime: Runtime, *, event_id: UUID | None = None) -> UUID:
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        identifier: Final = await append_native_audit(
            writer, actor_id="private-native-owner", action="native_key.rotated", event_id=event_id,
        )
        await writer.query_raw("UPDATE native_business SET credential='fixture-replacement-private-key' RETURNING id")
    assert identifier is not None
    return identifier


async def claim(runtime: Runtime, owner: str = "worker") -> NativeAuditClaim:
    selected: Final = await runtime.store.claim(owner)
    assert selected is not None
    return selected


async def due_now(runtime: Runtime) -> None:
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await writer.query_raw("UPDATE retail_native_audit_outbox SET next_attempt_at=clock_timestamp() RETURNING id")


async def retail_counts(runtime: Runtime) -> tuple[int, int]:
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (await connection.execute("""SELECT (SELECT count(*) FROM retail_operation_events) AS events,
            (SELECT count(*) FROM retail_audit_deliveries) AS deliveries""")).fetchone()
    assert row is not None
    return row["events"], row["deliveries"]


@pytest.mark.asyncio
async def test_native_transaction_rolls_back_both_managed_change_and_uncommitted_intent(runtime: Runtime) -> None:
    with pytest.raises(RuntimeError, match="fixture rollback"):
        async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
            await append_native_audit(writer, actor_id="owner", action="native_key.rotated")
            await writer.query_raw("UPDATE native_business SET credential='fixture-rolled-back-key' RETURNING id")
            assert await runtime.peer.claim("concurrent") is None
            raise RuntimeError("fixture rollback")
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        business: Final = await writer.query_raw("SELECT credential FROM native_business")
        settings: Final = await writer.query_raw("SELECT count(*) AS total FROM retail_native_audit_settings")
    assert business == [{"credential": "fixture-original-private-key"}]
    assert settings == [{"total": 0}]
    assert (await runtime.store.counts()).retained == 0
    assert await retail_counts(runtime) == (0, 0)


@pytest.mark.asyncio
async def test_schema_startup_is_ddl_only_and_preserves_existing_queue(runtime: Runtime) -> None:
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        empty: Final = await writer.query_raw("SELECT count(*) AS total FROM retail_native_audit_settings")
    assert empty == [{"total": 0}]
    identifier: Final = await native_change(runtime)
    await ensure_native_schema(runtime.native)
    selected: Final = await claim(runtime)
    assert selected.id == identifier and selected.attempts == 1


@pytest.mark.asyncio
async def test_native_success_is_durable_before_independent_retail_bridge_and_contains_no_credentials(runtime: Runtime) -> None:
    identifier: Final = await native_change(runtime)
    assert (await runtime.store.counts()).pending == 1
    assert await retail_counts(runtime) == (0, 0)
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        native_payload: Final = await writer.query_raw("SELECT to_jsonb(n) AS payload FROM retail_native_audit_outbox n")
        business: Final = await writer.query_raw("SELECT credential FROM native_business")
    assert business == [{"credential": "fixture-replacement-private-key"}]
    assert "private-key" not in str(native_payload)
    assert "fixture-original" not in str(native_payload)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("""UPDATE retail_audit_delivery_settings
            SET enabled=true,config_fingerprint=%s WHERE singleton""", ("a" * 64,))
    selected: Final = await claim(runtime)
    assert selected.id == identifier
    assert await runtime.store.bridge(selected) is True
    assert await runtime.store.acknowledge(selected) is True
    assert await retail_counts(runtime) == (1, 1)
    async with runtime.wallet.diagnostic_transaction() as connection:
        event: Final = await (await connection.execute("SELECT * FROM retail_operation_events")).fetchone()
        delivery: Final = await (await connection.execute("SELECT payload FROM retail_audit_deliveries")).fetchone()
    assert event is not None and delivery is not None
    assert event["organization_id"] is None and event["resource_id"] == str(identifier)
    assert event["changes"] == {"kind": "native_key.rotated", "row_count": 1}
    assert "private-key" not in str(event) + str(delivery)
    assert "private-native-owner" not in str(delivery)
    assert (await runtime.store.counts()).retained == 0


@pytest.mark.asyncio
async def test_cross_database_commit_ack_loss_replay_has_one_event_and_delivery(runtime: Runtime) -> None:
    await native_change(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("""UPDATE retail_audit_delivery_settings
            SET enabled=true,config_fingerprint=%s WHERE singleton""", ("a" * 64,))
    abandoned: Final = await claim(runtime, "crashed-process")
    assert await runtime.store.bridge(abandoned) is True
    await runtime.store.fail(abandoned)
    await due_now(runtime)
    retry: Final = await runtime.peer.claim("replacement-process")
    assert retry is not None and retry.id == abandoned.id and retry.attempts == 2
    assert await runtime.peer.bridge(retry) is False
    assert await runtime.peer.acknowledge(retry) is True
    assert await retail_counts(runtime) == (1, 1)
    assert await runtime.store.acknowledge(abandoned) is False


@pytest.mark.asyncio
async def test_two_workers_share_global_two_leases_and_stale_ack_cannot_delete_reclaimed_event(runtime: Runtime) -> None:
    await native_change(runtime)
    await native_change(runtime)
    await native_change(runtime)
    first: Final = await claim(runtime, "old-worker")
    second: Final = await runtime.peer.claim("other-worker")
    assert second is not None and second.id != first.id
    assert await runtime.store.claim("third-worker") is None
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await writer.query_raw("""UPDATE retail_native_audit_outbox
            SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1::uuid RETURNING id""", str(first.id))
    replacement: Final = await runtime.peer.claim("new-worker")
    assert replacement is not None and replacement.id == first.id
    assert replacement.lease_token != first.lease_token
    assert await runtime.store.acknowledge(first) is False
    await runtime.store.fail(first)
    assert (await runtime.store.counts()).processing == 2
    assert await runtime.peer.bridge(replacement) is True
    assert await runtime.peer.acknowledge(replacement) is True
    assert (await runtime.store.counts()).retained == 2


@pytest.mark.asyncio
async def test_concurrent_retail_replays_are_serialized_without_duplicate_effect(runtime: Runtime) -> None:
    await native_change(runtime)
    selected: Final = await claim(runtime)
    results: Final = await asyncio.gather(runtime.store.bridge(selected), runtime.peer.bridge(selected))
    assert sorted(results) == [False, True]
    assert await retail_counts(runtime) == (1, 0)


@pytest.mark.asyncio
async def test_queue_full_aborts_native_managed_write_instead_of_losing_audit(runtime: Runtime) -> None:
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await append_native_audit(writer, actor_id="seed", action="native_key.rotated")
        await writer.query_raw("""INSERT INTO retail_native_audit_outbox
            (id,source_id,actor_id,action,resource_id,affected_count)
            SELECT gen_random_uuid(),source_id,'seed','native_key.rotated',gen_random_uuid(),1
            FROM retail_native_audit_settings CROSS JOIN generate_series(1,9999) RETURNING id""")
        await writer.query_raw("UPDATE retail_native_audit_settings SET retained_count=10000 RETURNING source_id")
    with pytest.raises(NativeAuditUnavailable, match="full"):
        async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
            await writer.query_raw("UPDATE native_business SET credential='fixture-must-rollback' RETURNING id")
            await append_native_audit(writer, actor_id="owner", action="native_key.rotated")
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        business: Final = await writer.query_raw("SELECT credential FROM native_business")
    assert business == [{"credential": "fixture-original-private-key"}]
    assert (await runtime.store.counts()).retained == 10000
    assert await retail_counts(runtime) == (0, 0)


@pytest.mark.asyncio
async def test_repeated_append_is_idempotent_but_conflicting_metadata_aborts(runtime: Runtime) -> None:
    identifier: Final = await native_change(runtime)
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        assert await append_native_audit(writer, actor_id="private-native-owner", action="native_key.rotated",
                                         event_id=identifier) == identifier
    with pytest.raises(NativeAuditConflict):
        async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
            await append_native_audit(writer, actor_id="other-owner", action="native_key.rotated", event_id=identifier)
    assert (await runtime.store.counts()).retained == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", (("actor_id", "tampered"), ("action", "native_password.changed"),
                                      ("affected_count", 9), ("occurred_at", datetime(2020, 1, 1, tzinfo=timezone.utc))))
async def test_native_event_metadata_is_immutable_at_database_boundary(runtime: Runtime, field: str, value: object) -> None:
    identifier: Final = await native_change(runtime)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with await psycopg.AsyncConnection.connect(runtime.native.database_url) as connection:
            await connection.execute(sql.SQL("UPDATE retail_native_audit_outbox SET {}=%s WHERE id=%s").format(
                sql.Identifier(field)), (value, identifier))
    assert (await claim(runtime)).actor_id == "private-native-owner"


@pytest.mark.asyncio
async def test_failed_retention_and_exhausted_cancellation_keep_intent_for_explicit_repair(runtime: Runtime) -> None:
    identifier: Final = await native_change(runtime)
    first: Final = await claim(runtime)
    await runtime.store.fail(first, "integrity_conflict")
    assert (await runtime.store.counts()).failed == 1
    assert await runtime.store.claim("later") is None
    assert await runtime.store.requeue_failed(identifier) is True
    retry: Final = await claim(runtime)
    assert retry.id == first.id and retry.digest() == first.digest() and retry.attempts == 1
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await writer.query_raw("UPDATE retail_native_audit_outbox SET attempts=5 RETURNING id")
    await runtime.store.release_claims(retry.lease_owner)
    await runtime.store.maintain()
    assert (await runtime.store.counts()).failed == 1
    assert await runtime.store.requeue_failed(identifier) is True
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await writer.query_raw("""UPDATE retail_native_audit_outbox
            SET expires_at=clock_timestamp()-interval '1 second' RETURNING id""")
    await runtime.store.maintain()
    assert (await runtime.store.counts()).retained == (await runtime.store.counts()).failed == 1
    assert await runtime.store.requeue_failed(identifier) is True
    assert await NativeAuditWorker(runtime.store).run_once() == 1
    assert (await runtime.store.counts()).retained == 0
    assert await retail_counts(runtime) == (1, 0)


@pytest.mark.asyncio
async def test_delayed_bridge_retains_source_occurrence_and_default_audit_time_stays_current(runtime: Runtime) -> None:
    occurred: Final = datetime(2026, 1, 1, 2, 3, tzinfo=timezone(timedelta(hours=8)))
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        identifier: Final = uuid4()
        await writer.query_raw("INSERT INTO retail_native_audit_settings(singleton,source_id) VALUES(true,gen_random_uuid()) RETURNING source_id")
        await writer.query_raw("""INSERT INTO retail_native_audit_outbox
            (id,source_id,actor_id,action,resource_id,affected_count,occurred_at)
            SELECT $1::uuid,source_id,'owner','native_session.revoked',$1::uuid,2,$2::timestamptz
            FROM retail_native_audit_settings RETURNING id""", str(identifier), occurred)
        await writer.query_raw("UPDATE retail_native_audit_settings SET retained_count=1 RETURNING source_id")
    selected: Final = await claim(runtime)
    await runtime.store.bridge(selected)
    async with runtime.wallet.diagnostic_transaction() as connection:
        before: Final = await (await connection.execute("SELECT clock_timestamp() AS time")).fetchone()
        await append_operation_event(connection, actor_id="default-owner", action="native_password.changed",
            resource_type="native_operation", resource_id=str(uuid4()), organization_id=None,
            version_before=None, version_after=None, changes={"kind": "native_password.changed", "row_count": 1},
            idempotency_key="default-time", request_digest="fixture", response={})
        rows: Final = await (await connection.execute("SELECT actor_id,created_at FROM retail_operation_events ORDER BY actor_id")).fetchall()
        after: Final = await (await connection.execute("SELECT clock_timestamp() AS time")).fetchone()
    assert before is not None and after is not None
    assert rows[0]["actor_id"] == "default-owner" and before["time"] <= rows[0]["created_at"] <= after["time"]
    assert rows[1]["actor_id"] == "owner" and rows[1]["created_at"] == occurred


@pytest.mark.asyncio
async def test_worker_pauses_on_flag_and_closes_its_pool_without_closing_native_or_wallet(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await native_change(runtime)
    worker: Final = NativeAuditWorker(runtime.store)
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "false")
    assert await worker.run_once() == 0 and (await runtime.store.counts()).pending == 1
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    assert await worker.run_once() == 1
    await native_change(runtime)
    selected: Final = await runtime.store.claim(worker.owner)
    assert selected is not None
    await worker.close()
    assert (await runtime.peer.counts()).pending == 1
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        business: Final = await writer.query_raw("SELECT count(*) AS total FROM native_business")
    assert business == [{"total": 1}]
    assert await retail_counts(runtime) == (1, 0)


@pytest.mark.asyncio
async def test_retail_lock_outage_defers_bridge_without_rolling_back_native_success(runtime: Runtime) -> None:
    identifier: Final = await native_change(runtime)
    worker: Final = NativeAuditWorker(runtime.store)
    async with await psycopg.AsyncConnection.connect(runtime.wallet.database_url) as connection:
        await connection.execute("LOCK TABLE retail_operation_events IN ACCESS EXCLUSIVE MODE")
        assert await worker.run_once() == 1
        assert (await runtime.store.counts()).pending == 1
    assert await retail_counts(runtime) == (0, 0)
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        saved: Final = await writer.query_raw("SELECT id,error_code,attempts FROM retail_native_audit_outbox")
        business: Final = await writer.query_raw("SELECT credential FROM native_business")
    assert saved == [{"id": identifier, "error_code": "bridge_unavailable", "attempts": 1}]
    assert business == [{"credential": "fixture-replacement-private-key"}]
    await due_now(runtime)
    assert await worker.run_once() == 1
    assert await retail_counts(runtime) == (1, 0)
    assert (await runtime.store.counts()).retained == 0


@pytest.mark.asyncio
async def test_bridge_never_borrows_saturated_wallet_pools(runtime: Runtime) -> None:
    await native_change(runtime)
    async with runtime.wallet.transaction(), runtime.wallet.critical_transaction(), runtime.wallet.diagnostic_transaction():
        assert await NativeAuditWorker(runtime.store).run_once() == 1
    assert await retail_counts(runtime) == (1, 0)
    assert (await runtime.store.counts()).retained == 0


@pytest.mark.asyncio
async def test_mismatched_retail_receipt_is_retained_as_integrity_failure(runtime: Runtime) -> None:
    await native_change(runtime)
    selected: Final = await claim(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await append_operation_event(connection, actor_id=selected.actor_id, action="native_password.changed",
            resource_type="native_operation", resource_id=str(selected.resource_id), organization_id=None,
            version_before=None, version_after=None, changes={"kind": "native_password.changed", "row_count": 1},
            idempotency_key=selected.operation_key(), request_digest="fixture-conflicting-digest", response={})
    await runtime.store.release_claims(selected.lease_owner)
    assert await NativeAuditWorker(runtime.store).run_once() == 1
    assert (await runtime.store.counts()).failed == 1
    assert await retail_counts(runtime) == (1, 0)
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        saved: Final = await writer.query_raw("SELECT error_code FROM retail_native_audit_outbox")
    assert saved == [{"error_code": "integrity_conflict"}]


@pytest.mark.asyncio
async def test_native_slot_waiting_is_part_of_total_deadline(runtime: Runtime) -> None:
    async with await psycopg.AsyncConnection.connect(runtime.native.database_url) as connection:
        await connection.execute("LOCK TABLE retail_native_audit_outbox IN ACCESS EXCLUSIVE MODE")
        async with asyncio.timeout(7):
            outcomes: Final = await asyncio.gather(*(runtime.store.counts() for _ in range(32)), return_exceptions=True)
    assert any(isinstance(outcome, TimeoutError) for outcome in outcomes)
    assert all(isinstance(outcome, (TimeoutError, psycopg.errors.LockNotAvailable)) for outcome in outcomes)
    assert (await runtime.store.counts()).retained == 0


@pytest.mark.asyncio
async def test_three_action_native_outbox_upgrades_in_place_and_bridges_all_five_kinds(runtime: Runtime) -> None:
    legacy: Final[tuple[NativeAuditAction, ...]] = (
        "native_key.rotated", "native_password.changed", "native_session.revoked",
    )
    added: Final[tuple[NativeAuditAction, ...]] = ("native_identity.configured", "native_identity.signed_in")
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        await writer.execute_raw(
            "ALTER TABLE retail_native_audit_outbox DROP CONSTRAINT retail_native_audit_outbox_action_v2_check",
        )
        await writer.execute_raw(
            """ALTER TABLE retail_native_audit_outbox ADD CONSTRAINT retail_native_audit_outbox_action_check
               CHECK(action IN ('native_key.rotated','native_password.changed','native_session.revoked'))""",
        )
        for action in legacy:
            await append_native_audit(writer, actor_id="migration-owner", action=action)
        before: Final = await writer.query_raw("SELECT * FROM retail_native_audit_outbox ORDER BY id")
        settings_before: Final = await writer.query_raw("SELECT * FROM retail_native_audit_settings")
    with pytest.raises(psycopg.errors.CheckViolation):
        async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
            await append_native_audit(writer, actor_id="migration-owner", action=added[0])
    await ensure_native_schema(runtime.native)
    await ensure_native_schema(runtime.native)
    async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
        assert await writer.query_raw("SELECT * FROM retail_native_audit_outbox ORDER BY id") == before
        assert await writer.query_raw("SELECT * FROM retail_native_audit_settings") == settings_before
        for action in added:
            await append_native_audit(writer, actor_id="migration-owner", action=action)
    with pytest.raises(psycopg.errors.CheckViolation):
        async with runtime.native.tx(timeout=timedelta(seconds=5)) as writer:
            await writer.query_raw(
                """INSERT INTO retail_native_audit_outbox(id,source_id,actor_id,action,resource_id,affected_count)
                   SELECT gen_random_uuid(),source_id,'migration-owner','unsupported-kind',gen_random_uuid(),1
                   FROM retail_native_audit_settings RETURNING id""",
            )
    worker: Final = NativeAuditWorker(runtime.store)
    for _ in range(len(legacy) + len(added)):
        await worker.run_once()
    assert await retail_counts(runtime) == (len(legacy) + len(added), 0)
    assert (await runtime.store.counts()).retained == 0
    async with runtime.wallet.diagnostic_transaction() as connection:
        rows: Final = await (await connection.execute(
            "SELECT action,changes,organization_id FROM retail_operation_events ORDER BY action",
        )).fetchall()
    assert tuple(row["action"] for row in rows) == tuple(sorted((*legacy, *added)))
    assert all(row["organization_id"] is None for row in rows)
    assert all(row["changes"] == {"kind": row["action"], "row_count": 1} for row in rows)

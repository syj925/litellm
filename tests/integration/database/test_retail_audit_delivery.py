import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from litellm.proxy.retail.audit_delivery_store import AuditDeliveryError, AuditDeliveryStore, DeliveryClaim
from litellm.proxy.retail.control_plane_access import ControlPlaneActor
from litellm.proxy.retail.operation_events import append_operation_event
from litellm.proxy.retail.store import WalletStore

FP: Final = "a" * 64
OTHER_FP: Final = "b" * 64
ADMIN: Final = ControlPlaneActor("admin", "proxy_admin", True, True)


@dataclass(frozen=True, slots=True)
class Runtime:
    wallet: WalletStore
    store: AuditDeliveryStore
    peer: AuditDeliveryStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    address: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not address:
        pytest.skip("A dedicated audit delivery PostgreSQL service is required")
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    base: Final = conninfo_to_dict(address)
    name: Final = "retail_delivery_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    selected: Final = make_conninfo(**{**base, "dbname": name})
    wallet: Final = WalletStore(selected, max_connections=4, diagnostic_connections=3, critical_connections=2)
    store: Final = AuditDeliveryStore(wallet)
    peer: Final = AuditDeliveryStore(wallet)
    try:
        async with await psycopg.AsyncConnection.connect(selected) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await store.initialize()
        await peer.initialize()
        yield Runtime(wallet, store, peer)
    finally:
        await asyncio.gather(store.close(), peer.close())
        await wallet.aclose()
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def event(runtime: Runtime, *, key: str | None = None) -> None:
    async with runtime.wallet.diagnostic_transaction() as connection:
        await append_operation_event(
            connection,
            actor_id="private-actor",
            action="organization.created",
            resource_type="organization",
            resource_id="private-resource",
            organization_id="private-organization",
            version_before=None,
            version_after=1,
            changes={"name": "private-customer-name"},
            idempotency_key=key or str(uuid4()),
            request_digest="private-request-digest",
            response={"token": "private-response-token"},
        )


async def enabled(runtime: Runtime) -> None:
    await runtime.store.configure(True, 1, FP, ADMIN, "enable")


async def claimed(runtime: Runtime) -> DeliveryClaim:
    claim: Final = await runtime.store.claim("worker", FP)
    assert claim is not None
    return claim


async def due_now(runtime: Runtime) -> None:
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_audit_deliveries SET next_attempt_at=clock_timestamp()")


@pytest.mark.asyncio
async def test_disabled_delivery_keeps_local_audit_without_enqueuing(runtime: Runtime) -> None:
    await event(runtime)
    status: Final = await runtime.store.status(None)
    assert status.enabled is False
    assert status.retained_count == status.counts.pending == 0
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert row == {"total": 1}


@pytest.mark.asyncio
async def test_event_and_frozen_minimal_envelope_commit_together(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    claim: Final = await claimed(runtime)
    payload: Final = claim.payload.model_dump(mode="json")
    assert set(payload) == {
        "event_id",
        "action",
        "resource_type",
        "result",
        "version_before",
        "version_after",
        "occurred_at",
    }
    assert payload["event_id"] == claim.event_id
    assert payload["action"] == "organization.created" and payload["result"] == "succeeded"
    assert "private-" not in str(payload)
    page: Final = await runtime.store.list(FP)
    assert page.total == 1
    assert set(page.items[0].model_dump()) == {
        "id",
        "event_id",
        "action",
        "resource_type",
        "occurred_at",
        "state",
        "attempts",
        "version",
        "created_at",
        "completed_at",
        "error_code",
        "next_attempt_at",
        "configuration_matches",
    }
    assert FP not in page.model_dump_json()


@pytest.mark.asyncio
async def test_rolled_back_domain_operation_leaves_no_delivery(runtime: Runtime) -> None:
    await enabled(runtime)
    with pytest.raises(RuntimeError, match="fixture rollback"):
        async with runtime.wallet.diagnostic_transaction() as connection:
            await append_operation_event(
                connection,
                actor_id="admin",
                action="organization.updated",
                resource_type="organization",
                resource_id="fixture",
                organization_id=None,
                version_before=1,
                version_after=2,
                changes={"enabled": False},
                idempotency_key="rollback",
                request_digest="fixture",
                response={},
            )
            raise RuntimeError("fixture rollback")
    status: Final = await runtime.store.status(FP)
    assert status.retained_count == 0
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute(
                "SELECT count(*) AS total FROM retail_operation_events WHERE idempotency_key='rollback'"
            )
        ).fetchone()
    assert row == {"total": 0}


@pytest.mark.asyncio
async def test_configure_is_idempotent_and_its_audit_does_not_enqueue_recursively(runtime: Runtime) -> None:
    replies: Final = await asyncio.gather(
        runtime.store.configure(True, 1, FP, ADMIN, "same-operation"),
        runtime.peer.configure(True, 1, FP, ADMIN, "same-operation"),
    )
    assert replies[0] == replies[1]
    assert replies[0].config_version == 2 and replies[0].retained_count == 0
    with pytest.raises(AuditDeliveryError, match="settings_conflict"):
        await runtime.store.configure(False, 1, FP, ADMIN, "stale-operation")
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert row == {"total": 1}


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_configuration(runtime: Runtime) -> None:
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute(
            """CREATE FUNCTION reject_fixture_audit() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'fixture audit failure'; END; $$;
          CREATE TRIGGER reject_fixture_audit BEFORE INSERT ON retail_operation_events
          FOR EACH ROW EXECUTE FUNCTION reject_fixture_audit()""",
            prepare=False,
        )
    with pytest.raises(psycopg.errors.RaiseException):
        await runtime.store.configure(True, 1, FP, ADMIN, "rejected-audit")
    status: Final = await runtime.store.status(FP)
    assert status.enabled is False and status.config_version == 1 and status.retained_count == 0


@pytest.mark.asyncio
async def test_two_worker_stores_share_a_global_two_delivery_lease_limit(runtime: Runtime) -> None:
    await enabled(runtime)
    for _ in range(4):
        await event(runtime)
    claims: Final = await asyncio.gather(
        *(selected.claim(str(index), FP) for index, selected in enumerate((runtime.store, runtime.peer) * 4))
    )
    actual: Final = tuple(claim for claim in claims if claim is not None)
    assert len(actual) == 2
    assert len({claim.id for claim in actual}) == len({claim.lease_token for claim in actual}) == 2
    status: Final = await runtime.store.status(FP)
    assert status.counts.processing == status.counts.pending == 2
    assert runtime.store.pool.get_stats()["pool_available"] >= 1


@pytest.mark.asyncio
async def test_pause_stops_claiming_but_inflight_completion_is_allowed(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    claim: Final = await claimed(runtime)
    paused: Final = await runtime.store.configure(False, 2, FP, ADMIN, "pause")
    assert paused.enabled is False
    assert await runtime.peer.claim("other-worker", FP) is None
    assert await runtime.peer.complete(claim) is True
    assert await runtime.store.complete(claim) is False
    status: Final = await runtime.store.status(FP)
    assert status.counts.delivered == 1 and status.counts.processing == 0


@pytest.mark.asyncio
async def test_expired_lease_reclaim_rejects_the_old_owner_token(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    old: Final = await claimed(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_audit_deliveries SET lease_until=clock_timestamp()-interval '1 second'")
    assert await runtime.store.complete(old) is False
    current: Final = await runtime.peer.claim("replacement", FP)
    assert current is not None and current.id == old.id and current.lease_token != old.lease_token
    assert current.attempts == old.attempts + 1
    assert await runtime.store.fail(old, "timeout") is False
    assert await runtime.peer.complete(current) is True


@pytest.mark.asyncio
async def test_retry_backoff_and_five_attempt_bound_preserve_stable_delivery_identity(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    first: Final = await claimed(runtime)
    assert await runtime.store.fail(first, "http_retryable") is True
    assert await runtime.peer.claim("too-early", FP) is None
    for expected in range(2, 6):
        await due_now(runtime)
        current: Final = await claimed(runtime)
        assert current.id == first.id and current.event_id == first.event_id and current.attempts == expected
        assert await runtime.peer.fail(current, "timeout") is True
    page: Final = await runtime.store.list(FP, state="failed")
    assert page.total == 1 and page.items[0].attempts == 5 and page.items[0].error_code == "timeout"
    retried: Final = await runtime.store.retry(page.items[0].id, page.items[0].version, FP, ADMIN, "manual-retry")
    replay: Final = await runtime.peer.retry(page.items[0].id, page.items[0].version, FP, ADMIN, "manual-retry")
    assert retried == replay and retried.id == first.id and retried.state == "pending" and retried.attempts == 0
    after: Final = await claimed(runtime)
    assert after.id == first.id and after.attempts == 1
    assert (await runtime.store.status(FP)).retained_count == 1


@pytest.mark.asyncio
async def test_non_retryable_failure_becomes_terminal_immediately(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    claim: Final = await claimed(runtime)
    assert await runtime.store.fail(claim, "redirect_rejected", retryable=False) is True
    page: Final = await runtime.store.list(FP, state="failed")
    assert page.items[0].attempts == 1 and page.items[0].error_code == "redirect_rejected"
    assert await runtime.peer.claim("other", FP) is None


@pytest.mark.asyncio
async def test_rotation_requires_pause_and_discard_and_invalidates_inflight_ack(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    claim: Final = await claimed(runtime)
    with pytest.raises(AuditDeliveryError, match="pause_required"):
        await runtime.store.configure(True, 2, OTHER_FP, ADMIN, "rotate-running")
    paused: Final = await runtime.store.configure(False, 2, OTHER_FP, ADMIN, "pause")
    assert paused.configuration_matches is False
    with pytest.raises(AuditDeliveryError, match="outstanding_deliveries"):
        await runtime.store.configure(True, 3, OTHER_FP, ADMIN, "rotate-with-inflight")
    discarded: Final = await runtime.store.discard(3, "discard_pending", ADMIN, "discard", fingerprint=OTHER_FP)
    assert discarded.configuration_matches is False and discarded.counts.discarded == 1
    assert await runtime.peer.complete(claim) is False
    rotated: Final = await runtime.store.configure(True, 4, OTHER_FP, ADMIN, "rotate")
    assert rotated.configuration_matches is True
    await event(runtime)
    assert await runtime.store.claim("stale-config", FP) is None
    fresh: Final = await runtime.peer.claim("fresh-config", OTHER_FP)
    assert fresh is not None and fresh.config_fingerprint == OTHER_FP


@pytest.mark.asyncio
async def test_failed_old_configuration_also_requires_explicit_discard(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    claim: Final = await claimed(runtime)
    assert await runtime.store.fail(claim, "http_rejected", retryable=False)
    await runtime.store.configure(False, 2, FP, ADMIN, "pause")
    with pytest.raises(AuditDeliveryError, match="outstanding_deliveries"):
        await runtime.peer.configure(True, 3, OTHER_FP, ADMIN, "rotate")


@pytest.mark.asyncio
async def test_pending_seven_day_expiry_cannot_be_retried(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("""WITH original AS (DELETE FROM retail_audit_deliveries RETURNING *)
        INSERT INTO retail_audit_deliveries(id,event_id,config_fingerprint,payload,created_at)
        SELECT id,event_id,config_fingerprint,payload,clock_timestamp()-interval '8 days' FROM original""")
    assert await runtime.store.maintain() == 1
    page: Final = await runtime.store.list(FP)
    row: Final = page.items[0]
    assert row.state == "failed" and row.error_code == "expired"
    with pytest.raises(AuditDeliveryError, match="delivery_conflict"):
        await runtime.peer.retry(row.id, row.version, FP, ADMIN, "retry-expired")
    assert await runtime.peer.claim("other", FP) is None


@pytest.mark.asyncio
async def test_terminal_history_expires_without_deleting_local_audit(runtime: Runtime) -> None:
    await enabled(runtime)
    await event(runtime)
    assert await runtime.store.complete(await claimed(runtime))
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_audit_deliveries SET completed_at=clock_timestamp()-interval '8 days'")
    assert await runtime.store.maintain() == 1
    status: Final = await runtime.store.status(FP)
    assert status.retained_count == status.counts.delivered == 0
    async with runtime.wallet.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert row == {"total": 2}


@pytest.mark.asyncio
async def test_full_queue_records_gap_and_commits_local_audit_then_prunes_at_most_two_hundred(runtime: Runtime) -> None:
    await enabled(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("""INSERT INTO retail_operation_events
        (id,actor_id,action,resource_type,resource_id,result,idempotency_key,request_digest,response)
        SELECT gen_random_uuid()::text,'fixture','organization.created','organization','fixture','succeeded',
          'bulk-'||n::text,'fixture','{}'::jsonb FROM generate_series(1,10001) n""")
    full: Final = await runtime.store.status(FP)
    assert full.retained_count == full.queue_capacity == 10000 and full.overflow_count == 1
    assert full.last_overflow_at is not None
    await event(runtime)
    assert (await runtime.store.status(FP)).overflow_count == 2
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute("""UPDATE retail_audit_deliveries SET state='delivered',completed_at=clock_timestamp()
        WHERE id IN (SELECT id FROM retail_audit_deliveries ORDER BY created_at,id LIMIT 201)""")
        row: Final = await (
            await connection.execute("SELECT count(*) AS total FROM retail_operation_events")
        ).fetchone()
    assert row == {"total": 10003}
    assert await runtime.store.maintain() == 200
    after: Final = await runtime.store.status(FP)
    assert after.retained_count == 9800 and after.counts.delivered == 1 and after.overflow_count == 2
    await event(runtime)
    assert (await runtime.store.status(FP)).retained_count == 9801


@pytest.mark.asyncio
async def test_late_source_timestamp_does_not_skip_newly_committed_intent(runtime: Runtime) -> None:
    await enabled(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_operation_events
        (id,actor_id,action,resource_type,resource_id,result,idempotency_key,request_digest,response,created_at)
        VALUES(%s,'fixture','organization.created','organization','fixture','succeeded','old-time','fixture','{}',
          clock_timestamp()-interval '1 day')""",
            (str(uuid4()),),
        )
    claim: Final = await claimed(runtime)
    assert claim.payload.action == "organization.created"
    assert claim.payload.occurred_at < claim.lease_until


@pytest.mark.asyncio
async def test_unsupported_direct_event_never_enters_external_queue(runtime: Runtime) -> None:
    await enabled(runtime)
    async with runtime.wallet.diagnostic_transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_operation_events
        (id,actor_id,action,resource_type,resource_id,result,idempotency_key,request_digest,response)
        VALUES(%s,'fixture','unrecognized.action','organization','fixture','succeeded','unknown','fixture','{}')""",
            (str(uuid4()),),
        )
    assert (await runtime.store.status(FP)).retained_count == 0


@pytest.mark.asyncio
async def test_read_only_admin_cannot_enable_or_discard(runtime: Runtime) -> None:
    viewer: Final = ControlPlaneActor("viewer", "proxy_admin_viewer", True, False)
    with pytest.raises(HTTPException) as denied:
        await runtime.store.configure(True, 1, FP, viewer, "enable")
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as discarded:
        await runtime.store.discard(1, "discard_pending", viewer, "discard")
    assert discarded.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_delivery_retry_and_invalid_state_do_not_modify_settings(runtime: Runtime) -> None:
    await enabled(runtime)
    with pytest.raises(AuditDeliveryError, match="delivery_not_found"):
        await runtime.store.retry(UUID(int=0), 1, FP, ADMIN, "retry-missing")
    with pytest.raises(AuditDeliveryError, match="invalid_query"):
        await runtime.store.list(FP, limit=101)
    assert (await runtime.store.status(FP)).config_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignment",
    (
        "id=gen_random_uuid()",
        "event_id=gen_random_uuid()::text",
        "config_fingerprint=repeat('b',64)",
        'payload=payload||\'{"actor_id":"private-fixture"}\'::jsonb',
        "created_at=clock_timestamp()-interval '1 day'",
    ),
)
async def test_delivery_identity_and_envelope_cannot_be_rewritten(runtime: Runtime, assignment: str) -> None:
    await enabled(runtime)
    await event(runtime)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.wallet.diagnostic_transaction() as connection:
            await connection.execute(sql.SQL("UPDATE retail_audit_deliveries SET ") + sql.SQL(assignment))
    claim: Final = await claimed(runtime)
    assert await runtime.store.complete(claim)

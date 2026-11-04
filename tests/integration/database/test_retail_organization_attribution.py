import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Final
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from litellm.proxy.retail.budget_accounting import lock_budget_wallet
from litellm.proxy.retail.models import BudgetExceeded, InsufficientFunds, ParallelLimitExceeded
from litellm.proxy.retail.organization_attribution import RequestOrganizationSnapshot, initialize_schema
from litellm.proxy.retail.settlement_store import record_facts
from litellm.proxy.retail.settlement_worker import SettlementWorker
from litellm.proxy.retail.store import WalletStore


@dataclass(frozen=True, slots=True)
class AttributionRuntime:
    store: WalletStore
    peer: WalletStore


@pytest_asyncio.fixture(loop_scope="function")
async def runtime() -> AsyncIterator[AttributionRuntime]:
    database_url: Final = os.environ.get("RETAIL_SETTLEMENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("A dedicated organization-attribution PostgreSQL service is required")
    base: Final = conninfo_to_dict(database_url)
    name: Final = "retail_attribution_" + uuid4().hex
    with psycopg.connect(**base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    address: Final = make_conninfo(**{**base, "dbname": name})
    store: Final = WalletStore(address, max_connections=4, diagnostic_connections=3, critical_connections=2)
    peer: Final = WalletStore(address, max_connections=4, diagnostic_connections=3, critical_connections=2)
    try:
        async with await psycopg.AsyncConnection.connect(address) as connection:
            await connection.execute('CREATE TABLE "LiteLLM_UserTable"(user_id TEXT PRIMARY KEY)')
        await store.initialize()
        await peer.initialize()
        for user in ("account", "other-account"):
            await store.get_wallet(user, create=True)
            await store.credit(user, "1000", "seed-" + user, "fixture", "fixture", "seed-" + user)
        async with store.diagnostic_transaction() as connection:
            await connection.execute("""INSERT INTO retail_organizations(id,name,version)
                VALUES('org-a','Fixture A',2),('org-b','Fixture B',3)""")
            await connection.execute("""INSERT INTO retail_organization_memberships
                (organization_id,user_id,role,approved_by,version)
                VALUES('org-a','account','member','admin',4),('org-b','account','member','admin',5),
                ('org-a','other-account','member','admin',6),('org-b','other-account','member','admin',7)""")
        yield AttributionRuntime(store, peer)
    finally:
        await asyncio.gather(store.aclose(), peer.aclose())
        with psycopg.connect(**base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def assign(runtime: AttributionRuntime, organization_id: str | None, user_id: str = "account") -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, user_id)
        await connection.execute(
            """INSERT INTO retail_billing_organization_assignments
            (user_id,organization_id,created_by,updated_by) VALUES(%s,%s,'admin','admin')
            ON CONFLICT(user_id) DO UPDATE SET organization_id=EXCLUDED.organization_id,
            version=retail_billing_organization_assignments.version+1,updated_by='admin',updated_at=clock_timestamp()""",
            (user_id, organization_id),
        )


async def reserve(
    store: WalletStore, request_id: str, *, user_id: str = "account", snapshot: Mapping[str, JsonValue] | None = None
) -> Mapping[str, JsonValue]:
    return await store.reserve(user_id, request_id, "public-model", "0.4", snapshot or {}, max_parallel=100)


async def snapshot(runtime: AttributionRuntime, request_id: str) -> RequestOrganizationSnapshot | None:
    async with runtime.store.diagnostic_transaction() as connection:
        row: Final = await (
            await connection.execute(
                "SELECT * FROM retail_request_organization_snapshots WHERE request_id=%s", (request_id,)
            )
        ).fetchone()
    return RequestOrganizationSnapshot.model_validate(row) if row is not None else None


async def policy(runtime: AttributionRuntime, limit: int) -> None:
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(
            """INSERT INTO retail_model_budget_policies
            (id,user_id,model,ends_at,limit_nanos,created_by,updated_by)
            VALUES(%s,'account','public-model',clock_timestamp()+interval '1 hour',%s,'admin','admin')""",
            (uuid4(), limit),
        )


@pytest.mark.asyncio
async def test_multiple_memberships_do_not_implicitly_choose_a_billing_organization(runtime: AttributionRuntime) -> None:
    await reserve(runtime.store, "unassigned")
    assert await snapshot(runtime, "unassigned") is None
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.400000000"


@pytest.mark.asyncio
async def test_explicit_assignment_captures_current_versions_without_exposing_scope_in_reservation(
    runtime: AttributionRuntime,
) -> None:
    await assign(runtime, "org-a")
    result: Final = await reserve(runtime.store, "assigned")
    captured: Final = await snapshot(runtime, "assigned")
    assert captured is not None
    assert (
        captured.request_id,
        captured.organization_id,
        captured.assignment_version,
        captured.organization_version,
        captured.membership_version,
    ) == ("assigned", "org-a", 1, 2, 4)
    assert "organization_id" not in result
    assert result["price_snapshot"] == {}


@pytest.mark.asyncio
async def test_moving_assignment_does_not_move_late_charge_or_duplicate_its_snapshot(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "before-move")
    first: Final = await snapshot(runtime, "before-move")
    await assign(runtime, "org-b")
    await reserve(runtime.peer, "after-move")
    await asyncio.gather(*(runtime.store.settle("before-move", "0.25", {}) for _ in range(4)))
    await runtime.peer.settle("after-move", "0.15", {})
    assert await snapshot(runtime, "before-move") == first
    second: Final = await snapshot(runtime, "after-move")
    assert second is not None and (second.organization_id, second.assignment_version) == ("org-b", 2)
    async with runtime.store.diagnostic_transaction() as connection:
        totals: Final = await (
            await connection.execute("""SELECT s.organization_id,sum(-l.amount_nanos) AS charged,count(*) AS rows
            FROM retail_request_organization_snapshots s JOIN retail_ledger l USING(request_id)
            WHERE l.kind='charge' GROUP BY s.organization_id ORDER BY s.organization_id""")
        ).fetchall()
    assert totals == [
        {"organization_id": "org-a", "charged": 250000000, "rows": 1},
        {"organization_id": "org-b", "charged": 150000000, "rows": 1},
    ]


@pytest.mark.asyncio
async def test_clear_assignment_leaves_history_and_future_requests_unattributed(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "before-clear")
    first: Final = await snapshot(runtime, "before-clear")
    await assign(runtime, None)
    await reserve(runtime.peer, "after-clear")
    assert await snapshot(runtime, "before-clear") == first
    assert await snapshot(runtime, "after-clear") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("organization", "membership"))
async def test_disabled_organization_or_member_stops_new_attribution_without_blocking_wallet(
    runtime: AttributionRuntime, target: str
) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "before-disable")
    first: Final = await snapshot(runtime, "before-disable")
    statement: Final = (
        "UPDATE retail_organizations SET enabled=false,version=version+1 WHERE id='org-a'"
        if target == "organization"
        else "UPDATE retail_organization_memberships SET enabled=false,version=version+1 "
        "WHERE organization_id='org-a' AND user_id='account'"
    )
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(statement)
    result: Final = await reserve(runtime.peer, "after-disable")
    assert result["status"] == "reserved"
    assert await snapshot(runtime, "before-disable") == first
    assert await snapshot(runtime, "after-disable") is None


@pytest.mark.asyncio
async def test_missing_membership_does_not_attribute_to_an_unrelated_organization(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute(
            "DELETE FROM retail_organization_memberships WHERE organization_id='org-a' AND user_id='account'"
        )
    await reserve(runtime.store, "without-member")
    assert await snapshot(runtime, "without-member") is None


@pytest.mark.asyncio
async def test_existing_unassigned_request_replay_and_late_settlement_cannot_backfill_scope(
    runtime: AttributionRuntime,
) -> None:
    original: Final = await reserve(runtime.store, "legacy-request")
    await assign(runtime, "org-a")
    assert await reserve(runtime.peer, "legacy-request") == original
    await runtime.store.settle("legacy-request", "0.25", {})
    assert await snapshot(runtime, "legacy-request") is None


@pytest.mark.asyncio
async def test_released_then_late_valid_charge_stays_with_original_organization(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "released-late")
    captured: Final = await snapshot(runtime, "released-late")
    await runtime.store.release("released-late", "upstream rejection")
    await assign(runtime, "org-b")
    await record_facts(runtime.store, "released-late", "0.25", {"usage": "late"})
    await SettlementWorker(runtime.store).run_once()
    assert await snapshot(runtime, "released-late") == captured
    result: Final = await runtime.store.get_reservation("released-late")
    assert result is not None and result["status"] == "settled"


@pytest.mark.asyncio
async def test_unknown_and_conflicting_facts_keep_the_admitted_organization(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "unknown")
    await record_facts(runtime.store, "unknown", None, {"billing_error": "missing usage"})
    await reserve(runtime.store, "conflict")
    await runtime.store.release(
        "conflict", "reviewed", {"actor_id": "admin", "reason": "reviewed", "reference": "manual", "idempotency_key": "manual"}
    )
    await record_facts(runtime.store, "conflict", "0.2", {"usage": "late"})
    await assign(runtime, "org-b")
    await SettlementWorker(runtime.store).run_once()
    async with runtime.store.diagnostic_transaction() as connection:
        rows: Final = await (
            await connection.execute("""SELECT s.organization_id,f.request_id,f.state FROM
            retail_request_organization_snapshots s JOIN retail_settlement_facts f USING(request_id)
            ORDER BY f.request_id""")
        ).fetchall()
    assert rows == [
        {"organization_id": "org-a", "request_id": "conflict", "state": "conflict"},
        {"organization_id": "org-a", "request_id": "unknown", "state": "unknown"},
    ]


@pytest.mark.asyncio
async def test_account_assignment_is_shared_across_keys_pools_and_models_but_not_other_accounts(
    runtime: AttributionRuntime,
) -> None:
    await assign(runtime, "org-a")
    await assign(runtime, "org-b", "other-account")
    await reserve(runtime.store, "pool-a", snapshot={"resource_pool_id": "pool-a", "credential": "fixture-a"})
    await runtime.peer.reserve(
        "account", "pool-b", "another-public-model", "0.4", {"resource_pool_id": "pool-b", "credential": "fixture-b"}
    )
    await reserve(runtime.peer, "other-account", user_id="other-account")
    values: Final = await asyncio.gather(
        *(snapshot(runtime, identifier) for identifier in ("pool-a", "pool-b", "other-account"))
    )
    assert all(value is not None for value in values)
    assert tuple(value.organization_id for value in values if value is not None) == ("org-a", "org-a", "org-b")


@pytest.mark.asyncio
async def test_management_flags_do_not_disable_a_saved_assignment(
    runtime: AttributionRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "false")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "false")
    await assign(runtime, "org-a")
    await reserve(runtime.peer, "flags-off")
    captured: Final = await snapshot(runtime, "flags-off")
    assert captured is not None and captured.organization_id == "org-a"


@pytest.mark.asyncio
async def test_snapshot_failure_rolls_back_reservation_budget_and_wallet(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await policy(runtime, 1000000000)
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("""CREATE FUNCTION reject_fixture_attribution() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Attribution fixture rejects write'; END; $$""")
        await connection.execute("""CREATE TRIGGER reject_fixture_attribution BEFORE INSERT
            ON retail_request_organization_snapshots FOR EACH ROW EXECUTE FUNCTION reject_fixture_attribution()""")
    with pytest.raises(psycopg.errors.RaiseException, match="Attribution fixture rejects write"):
        await reserve(runtime.peer, "rollback")
    assert await snapshot(runtime, "rollback") is None
    assert await runtime.store.get_reservation("rollback") is None
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.000000000"
    async with runtime.store.diagnostic_transaction() as connection:
        budget: Final = await (
            await connection.execute("SELECT held_nanos,spent_nanos,accounting_version FROM retail_model_budget_policies")
        ).fetchone()
    assert budget == {"held_nanos": 0, "spent_nanos": 0, "accounting_version": 0}


@pytest.mark.asyncio
async def test_budget_refusal_does_not_leave_an_attribution_row(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await policy(runtime, 100000000)
    with pytest.raises(BudgetExceeded):
        await reserve(runtime.store, "over-budget")
    assert await snapshot(runtime, "over-budget") is None
    assert await runtime.store.get_reservation("over-budget") is None


@pytest.mark.asyncio
async def test_funds_and_parallel_refusals_do_not_leave_an_attribution_row(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    with pytest.raises(InsufficientFunds):
        await runtime.store.reserve("account", "no-funds", "public-model", "1001", {})
    await reserve(runtime.store, "in-flight")
    with pytest.raises(ParallelLimitExceeded):
        await runtime.peer.reserve("account", "no-slot", "public-model", "0.1", {}, max_parallel=1)
    assert await snapshot(runtime, "no-funds") is None
    assert await snapshot(runtime, "no-slot") is None
    assert (await runtime.store.get_wallet("account"))["reserved"] == "0.400000000"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("request_id", "rewritten-request"),
        ("organization_id", "org-b"),
        ("assignment_version", 99),
        ("organization_version", 99),
        ("membership_version", 99),
    ),
)
async def test_snapshot_identity_and_versions_cannot_be_rewritten(
    runtime: AttributionRuntime, column: str, value: str | int
) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "immutable")
    original: Final = await snapshot(runtime, "immutable")
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.store.diagnostic_transaction() as connection:
            await connection.execute(
                sql.SQL("UPDATE retail_request_organization_snapshots SET {}=%s WHERE request_id='immutable'").format(
                    sql.Identifier(column)
                ),
                (value,),
            )
    assert await snapshot(runtime, "immutable") == original


@pytest.mark.asyncio
async def test_snapshot_timestamp_cannot_be_rewritten_and_record_cannot_be_deleted(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    await reserve(runtime.store, "immutable")
    original: Final = await snapshot(runtime, "immutable")
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.store.diagnostic_transaction() as connection:
            await connection.execute("""UPDATE retail_request_organization_snapshots
                SET captured_at=captured_at+interval '1 day' WHERE request_id='immutable'""")
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        async with runtime.store.diagnostic_transaction() as connection:
            await connection.execute("DELETE FROM retail_request_organization_snapshots WHERE request_id='immutable'")
    assert await snapshot(runtime, "immutable") == original


@pytest.mark.asyncio
async def test_versions_and_capture_clock_are_frozen_at_admission(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    async with runtime.store.diagnostic_transaction() as connection:
        before: Final = await (await connection.execute("SELECT clock_timestamp() AS at")).fetchone()
    await reserve(runtime.store, "old-versions")
    first: Final = await snapshot(runtime, "old-versions")
    async with runtime.store.diagnostic_transaction() as connection:
        await connection.execute("UPDATE retail_organizations SET version=version+1 WHERE id='org-a'")
        await connection.execute("""UPDATE retail_organization_memberships SET version=version+1
            WHERE organization_id='org-a' AND user_id='account'""")
    await reserve(runtime.peer, "new-versions")
    second: Final = await snapshot(runtime, "new-versions")
    assert before is not None and first is not None and second is not None
    assert before["at"] <= first.captured_at <= second.captured_at
    assert (first.organization_version, first.membership_version) == (2, 4)
    assert (second.organization_version, second.membership_version) == (3, 5)
    assert await snapshot(runtime, "old-versions") == first


@pytest.mark.asyncio
async def test_schema_upgrade_does_not_rewrite_or_backfill_existing_wallet_history(runtime: AttributionRuntime) -> None:
    await reserve(runtime.store, "old-history")
    await runtime.store.settle("old-history", "0.25", {})
    query: Final = """SELECT (SELECT jsonb_agg(to_jsonb(w) ORDER BY user_id) FROM retail_wallets w) AS wallets,
        (SELECT jsonb_agg(to_jsonb(r) ORDER BY request_id) FROM retail_reservations r) AS reservations,
        (SELECT jsonb_agg(to_jsonb(l) ORDER BY id) FROM retail_ledger l) AS ledger"""
    async with runtime.store.diagnostic_transaction() as connection:
        before: Final = await (await connection.execute(query)).fetchone()
        await connection.execute("DROP TABLE retail_request_organization_snapshots")
        await connection.execute("DROP TABLE retail_billing_organization_assignments")
        await initialize_schema(connection)
        after: Final = await (await connection.execute(query)).fetchone()
        counts: Final = await (
            await connection.execute("""SELECT
            (SELECT count(*) FROM retail_billing_organization_assignments) AS assignments,
            (SELECT count(*) FROM retail_request_organization_snapshots) AS snapshots""")
        ).fetchone()
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert counts == {"assignments": 0, "snapshots": 0}


async def wait_for_wallet_waiter(connection: psycopg.AsyncConnection) -> None:
    async with asyncio.timeout(2):
        while True:
            await connection.execute("SELECT pg_stat_clear_snapshot()")
            waiting: Final = await (
                await connection.execute("""SELECT count(*) AS total FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                AND wait_event_type='Lock' AND query LIKE '%%retail_wallets%%'""")
            ).fetchone()
            if waiting is not None and waiting["total"]:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_wallet_lock_wait_observes_the_assignment_committed_before_admission(runtime: AttributionRuntime) -> None:
    await assign(runtime, "org-a")
    async with runtime.store.diagnostic_transaction() as connection:
        await lock_budget_wallet(connection, "account")
        task: Final = asyncio.create_task(reserve(runtime.peer, "after-lock"))
        await wait_for_wallet_waiter(connection)
        await connection.execute("""UPDATE retail_billing_organization_assignments
            SET organization_id='org-b',version=version+1 WHERE user_id='account'""")
    await task
    captured: Final = await snapshot(runtime, "after-lock")
    assert captured is not None and (captured.organization_id, captured.assignment_version) == ("org-b", 2)

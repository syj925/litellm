from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict

from .models import WalletNotFound


class BudgetPolicyRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    predecessor_id: UUID | None = None
    user_id: str
    model: str
    effective_from: datetime
    ends_at: datetime
    limit_nanos: int
    spent_nanos: int
    held_nanos: int
    enabled: bool
    config_version: int
    accounting_version: int
    source: str
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str
    accounted_at: datetime | None


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    await connection.execute(Path(__file__).with_name("budget_schema.sql").read_text(encoding="utf-8"), prepare=False)
    await connection.execute(Path(__file__).with_name("monthly_budget_schema.sql").read_text(encoding="utf-8"), prepare=False)


async def lock_budget_wallet(connection: psycopg.AsyncConnection, user_id: str) -> None:
    cursor: Final = await connection.execute(
        "SELECT user_id FROM retail_wallets WHERE user_id=%s FOR UPDATE", (user_id,)
    )
    if await cursor.fetchone() is None:
        raise WalletNotFound("Wallet is not enabled for this user")


RESERVE_SQL: Final = """WITH admission AS MATERIALIZED (SELECT clock_timestamp() AS at),
    scope AS MATERIALIZED (SELECT %s::text AS user_id,%s::text AS model),
    policy AS MATERIALIZED (
      SELECT p.id,p.enabled,p.limit_nanos,p.spent_nanos,p.held_nanos,p.config_version
      FROM retail_model_budget_policies p CROSS JOIN admission a
      CROSS JOIN scope s WHERE p.user_id=s.user_id AND p.model=s.model AND p.effective_from<=a.at AND p.ends_at>a.at
      AND NOT EXISTS(SELECT 1 FROM retail_model_budget_occurrences o JOIN retail_model_budget_recurrences r
        ON r.id=o.recurrence_id WHERE o.policy_id=p.id AND r.stops_at IS NOT NULL AND p.effective_from>=r.stops_at)
      FOR UPDATE OF p
    ), expectation AS MATERIALIZED (
      SELECT NOT EXISTS(SELECT 1 FROM retail_model_budget_recurrences r CROSS JOIN scope s CROSS JOIN admission a
        WHERE r.user_id=s.user_id AND r.model=s.model AND r.starts_at<=a.at AND (r.stops_at IS NULL OR r.stops_at>a.at)
        AND NOT EXISTS(SELECT 1 FROM retail_model_budget_occurrences o JOIN policy p ON p.id=o.policy_id
          WHERE o.recurrence_id=r.id AND p.enabled AND o.starts_at=
            (date_trunc('month',a.at AT TIME ZONE r.timezone) AT TIME ZONE r.timezone))) AS ready
    ), budget AS (
      UPDATE retail_model_budget_policies p SET held_nanos=p.held_nanos+%s,
        accounting_version=p.accounting_version+1,accounted_at=clock_timestamp()
      FROM policy b WHERE p.id=b.id AND (SELECT ready FROM expectation)
        AND (NOT b.enabled OR b.limit_nanos-b.spent_nanos-b.held_nanos>=%s)
      RETURNING p.id,p.config_version
    ), held AS (
      UPDATE retail_wallets SET reserved_nanos=reserved_nanos+%s,updated_at=now()
      WHERE user_id=%s AND (SELECT ready FROM expectation)
        AND (NOT EXISTS(SELECT 1 FROM policy) OR EXISTS(SELECT 1 FROM budget)) RETURNING user_id
    ), reservation AS (
      INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot,active_until)
      SELECT %s,held.user_id,%s,%s,%s,now()+(%s * interval '1 second') FROM held RETURNING *
    ), attribution AS (
      INSERT INTO retail_request_organization_snapshots
        (request_id,organization_id,assignment_version,organization_version,membership_version,captured_at)
      SELECT r.request_id,o.id,a.version,o.version,m.version,t.at
      FROM reservation r JOIN retail_billing_organization_assignments a ON a.user_id=r.user_id
      JOIN retail_organizations o ON o.id=a.organization_id AND o.enabled=true
      JOIN retail_organization_memberships m ON m.organization_id=o.id AND m.user_id=r.user_id AND m.enabled=true
      CROSS JOIN admission t RETURNING request_id
    ), allocation AS (
      INSERT INTO retail_model_budget_allocations(request_id,policy_id,reserved_nanos,status,config_version)
      SELECT r.request_id,b.id,r.reserved_nanos,'held',b.config_version FROM reservation r CROSS JOIN budget b
      RETURNING request_id
    ) SELECT * FROM reservation"""

SETTLE_WALLET_SQL: Final = """WITH allocation AS MATERIALIZED (
      SELECT request_id,policy_id,reserved_nanos,status FROM retail_model_budget_allocations
      WHERE request_id=%s FOR UPDATE
    ), budget AS (
      UPDATE retail_model_budget_policies p SET
        held_nanos=p.held_nanos-CASE WHEN a.status='held' THEN a.reserved_nanos ELSE 0 END,
        spent_nanos=p.spent_nanos+%s,accounting_version=p.accounting_version+1,accounted_at=clock_timestamp()
      FROM allocation a WHERE p.id=a.policy_id AND a.status IN ('held','released') RETURNING p.id
    ), closed AS (
      UPDATE retail_model_budget_allocations a SET status='settled',settled_nanos=%s,updated_at=clock_timestamp()
      FROM budget b WHERE a.policy_id=b.id AND a.request_id=%s RETURNING a.request_id
    ) UPDATE retail_wallets SET balance_nanos=balance_nanos-%s,reserved_nanos=reserved_nanos-%s,
      total_spent_nanos=total_spent_nanos+%s,updated_at=now() WHERE user_id=%s RETURNING balance_nanos"""

RELEASE_WALLET_SQL: Final = """WITH allocation AS MATERIALIZED (
      SELECT request_id,policy_id,reserved_nanos FROM retail_model_budget_allocations
      WHERE request_id=%s AND status='held' FOR UPDATE
    ), budget AS (
      UPDATE retail_model_budget_policies p SET held_nanos=p.held_nanos-a.reserved_nanos,
        accounting_version=p.accounting_version+1,accounted_at=clock_timestamp()
      FROM allocation a WHERE p.id=a.policy_id RETURNING p.id
    ), closed AS (
      UPDATE retail_model_budget_allocations a SET status='released',updated_at=clock_timestamp()
      FROM budget b WHERE a.policy_id=b.id AND a.request_id=%s RETURNING a.request_id
    ) UPDATE retail_wallets SET reserved_nanos=reserved_nanos-%s,updated_at=now()
      WHERE user_id=%s RETURNING balance_nanos"""

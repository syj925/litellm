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


async def lock_budget_wallet(connection: psycopg.AsyncConnection, user_id: str) -> None:
    cursor: Final = await connection.execute(
        "SELECT user_id FROM retail_wallets WHERE user_id=%s FOR UPDATE", (user_id,)
    )
    if await cursor.fetchone() is None:
        raise WalletNotFound("Wallet is not enabled for this user")


RESERVE_SQL: Final = """WITH admission AS MATERIALIZED (SELECT clock_timestamp() AS at),
    policy AS MATERIALIZED (
      SELECT p.id,p.enabled,p.limit_nanos,p.spent_nanos,p.held_nanos,p.config_version
      FROM retail_model_budget_policies p CROSS JOIN admission a
      WHERE p.user_id=%s AND p.model=%s AND p.effective_from<=a.at AND p.ends_at>a.at FOR UPDATE OF p
    ), budget AS (
      UPDATE retail_model_budget_policies p SET held_nanos=p.held_nanos+%s,
        accounting_version=p.accounting_version+1,accounted_at=clock_timestamp()
      FROM policy b WHERE p.id=b.id AND (NOT b.enabled OR b.limit_nanos-b.spent_nanos-b.held_nanos>=%s)
      RETURNING p.id,p.config_version
    ), held AS (
      UPDATE retail_wallets SET reserved_nanos=reserved_nanos+%s,updated_at=now()
      WHERE user_id=%s AND (NOT EXISTS(SELECT 1 FROM policy) OR EXISTS(SELECT 1 FROM budget)) RETURNING user_id
    ), reservation AS (
      INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot,active_until)
      SELECT %s,held.user_id,%s,%s,%s,now()+(%s * interval '1 second') FROM held RETURNING *
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

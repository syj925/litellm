import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, JsonValue

from .models import IdempotencyConflict, ReservationNotFound, cost_to_nanos
from .store import WalletStore


class SettlementFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    amount_nanos: int | None
    details: Mapping[str, JsonValue]
    fact_digest: str
    state: str
    attempts: int
    lease_owner: str | None = None


def fact_digest(amount_nanos: int | None, details: Mapping[str, JsonValue]) -> str:
    semantic: Final = {key: value for key, value in details.items() if key not in {"provider_cost", "gateway_call_id"}}
    encoded: Final = json.dumps(
        {"amount_nanos": amount_nanos, "details": semantic}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


async def record_facts(
    store: WalletStore,
    request_id: str,
    amount: str | None,
    details: Mapping[str, JsonValue],
) -> SettlementFact:
    await store.initialize()
    nanos: Final = cost_to_nanos(amount) if amount is not None else None
    digest: Final = fact_digest(nanos, details)
    async with store.critical_transaction() as connection:
        cursor: Final = await connection.execute(
            """INSERT INTO retail_settlement_facts(request_id,amount_nanos,details,fact_digest,state)
            SELECT request_id,%s,%s,%s,%s FROM retail_reservations WHERE request_id=%s
            ON CONFLICT(request_id) DO UPDATE SET
              amount_nanos=COALESCE(retail_settlement_facts.amount_nanos,EXCLUDED.amount_nanos),
              details=CASE WHEN retail_settlement_facts.amount_nanos IS NULL THEN EXCLUDED.details
                ELSE retail_settlement_facts.details END,
              fact_digest=CASE WHEN retail_settlement_facts.amount_nanos IS NULL THEN EXCLUDED.fact_digest
                ELSE retail_settlement_facts.fact_digest END,
              state=CASE WHEN retail_settlement_facts.amount_nanos IS NULL AND EXCLUDED.amount_nanos IS NOT NULL
                THEN 'pending' ELSE retail_settlement_facts.state END,
              updated_at=now()
            WHERE retail_settlement_facts.fact_digest=EXCLUDED.fact_digest OR retail_settlement_facts.amount_nanos IS NULL
            RETURNING *""",
            (nanos, Jsonb(dict(details)), digest, "pending" if nanos is not None else "unknown", request_id),
        )
        row: Final = await cursor.fetchone()
        if row is None:
            existing_cursor: Final = await connection.execute(
                "SELECT * FROM retail_settlement_facts WHERE request_id=%s",
                (request_id,),
            )
            existing: Final = await existing_cursor.fetchone()
            if existing is None:
                raise ReservationNotFound("Request reservation does not exist")
            if nanos is not None:
                raise IdempotencyConflict("This request already has different durable billing facts")
            result: Final = SettlementFact.model_validate(existing)
        else:
            result = SettlementFact.model_validate(row)
        await connection.execute(
            "UPDATE retail_reservations SET active_until=NULL WHERE request_id=%s AND active_until IS NOT NULL",
            (request_id,),
        )
    return result


async def frozen_pricing(store: WalletStore, request_id: str) -> Mapping[str, JsonValue] | None:
    async with store.critical_transaction() as connection:
        cursor: Final = await connection.execute(
            "SELECT price_snapshot FROM retail_reservations WHERE request_id=%s",
            (request_id,),
        )
        row: Final = await cursor.fetchone()
    return row["price_snapshot"] if row is not None else None


async def claim_facts(store: WalletStore, owner: str, limit: int, lease_seconds: int) -> tuple[SettlementFact, ...]:
    async with store.transaction() as connection:
        cursor: Final = await connection.execute(
            """WITH ready AS (
              SELECT request_id FROM retail_settlement_facts
              WHERE amount_nanos IS NOT NULL AND next_attempt_at<=now()
                AND (state='pending' OR (state='processing' AND lease_until<=now()))
              ORDER BY next_attempt_at,created_at FOR UPDATE SKIP LOCKED LIMIT %s
            ) UPDATE retail_settlement_facts f SET state='processing',lease_owner=%s,
              lease_until=now()+(%s * interval '1 second'),attempts=attempts+1,updated_at=now()
              FROM ready WHERE f.request_id=ready.request_id RETURNING f.*""",
            (limit, owner, lease_seconds),
        )
        rows: Final = await cursor.fetchall()
    return tuple(SettlementFact.model_validate(row) for row in rows)


async def finish_fact(store: WalletStore, fact: SettlementFact, error_type: str | None = None) -> None:
    async with store.transaction() as connection:
        await connection.execute(
            """UPDATE retail_settlement_facts SET state=%s,lease_owner=NULL,lease_until=NULL,
              last_error_type=%s,updated_at=now() WHERE request_id=%s AND state='processing' AND lease_owner=%s""",
            ("conflict" if error_type else "done", error_type, fact.request_id, fact.lease_owner),
        )


async def retry_fact(store: WalletStore, fact: SettlementFact, error_type: str) -> None:
    delay: Final = min(60, 2 ** min(fact.attempts, 6))
    async with store.transaction() as connection:
        await connection.execute(
            """UPDATE retail_settlement_facts SET state='pending',lease_owner=NULL,lease_until=NULL,
              next_attempt_at=now()+(%s * interval '1 second'),last_error_type=%s,updated_at=now()
              WHERE request_id=%s AND state='processing' AND lease_owner=%s""",
            (delay, error_type[:120], fact.request_id, fact.lease_owner),
        )


async def release_claims(store: WalletStore, owner: str) -> None:
    async with store.transaction() as connection:
        await connection.execute(
            """UPDATE retail_settlement_facts SET state='pending',lease_owner=NULL,lease_until=NULL,
              next_attempt_at=now(),updated_at=now() WHERE state='processing' AND lease_owner=%s""",
            (owner,),
        )


async def renew_activity(store: WalletStore, request_ids: Sequence[str], lease_seconds: int) -> None:
    if not request_ids:
        return
    async with store.transaction() as connection:
        await connection.execute(
            """UPDATE retail_reservations SET active_until=now()+(%s * interval '1 second')
              WHERE request_id=ANY(%s) AND status='reserved' AND active_until IS NOT NULL""",
            (lease_seconds, list(request_ids)),
        )


async def finish_activity(store: WalletStore, request_id: str) -> None:
    async with store.transaction() as connection:
        await connection.execute(
            "UPDATE retail_reservations SET active_until=NULL WHERE request_id=%s AND active_until IS NOT NULL",
            (request_id,),
        )

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Final, TypeVar
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, JsonValue

from .models import (
    IdempotencyConflict,
    InsufficientFunds,
    InvalidAmount,
    ParallelLimitExceeded,
    PlanNotFound,
    PricePlan,
    ReservationNotFound,
    ReservationView,
    WalletNotFound,
    WalletView,
    amount_to_nanos,
    cost_to_nanos,
    nanos_to_amount,
    parse_discount,
)


class _WalletRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    balance_nanos: int
    reserved_nanos: int
    total_credited_nanos: int
    total_spent_nanos: int
    plan_id: str
    plan_name: str
    multiplier: Decimal
    discount_override: Decimal | None = None
    pending_requests: int


class _PlanRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    multiplier: Decimal


class _ModelDiscountRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    discount: Decimal


class _ModelDiscountFactors(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    plan_id: str
    customer_discount: Decimal | None
    plan_discount: Decimal | None
    legacy_customer: Decimal | None
    legacy_plan: Decimal


class _ReservationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    user_id: str
    model: str
    reserved_nanos: int
    settled_nanos: int | None
    status: str
    price_snapshot: dict[str, JsonValue]
    details: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


class _LedgerRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    kind: str
    amount_nanos: int
    balance_after_nanos: int
    payload: dict[str, JsonValue]
    reference: str
    description: str
    actor_id: str
    request_id: str | None
    model: str | None
    details: dict[str, JsonValue]
    created_at: datetime


class _CountRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int


class _CreditOperationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    payload: dict[str, JsonValue]


class _ModelPriceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    input_cost_per_token: Decimal
    output_cost_per_token: Decimal
    cache_read_input_token_cost: Decimal
    cache_creation_input_token_cost: Decimal
    version: int
    updated_at: datetime
    source: str = "legacy"
    catalog_model: str | None = None
    source_label: str = "Existing reference, review required"


class _UserIdRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str


_Record = TypeVar("_Record", bound=BaseModel)
_WALLET_SELECT: Final = """
    SELECT w.*, p.name AS plan_name, p.multiplier,
           (SELECT count(*) FROM retail_reservations r
            WHERE r.user_id=w.user_id AND r.status='reserved') AS pending_requests
    FROM retail_wallets w JOIN retail_pricing_plans p ON p.id=w.plan_id
"""
_LEDGER_SELECT: Final = """
    SELECT id::text AS id,user_id,kind,amount_nanos,balance_after_nanos,payload,
           reference,description,actor_id,request_id,model,details,created_at
    FROM retail_ledger
"""
_MODEL_PRICE_KEYS: Final = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)
_MODEL_PRICE_INSERT: Final = """
    INSERT INTO retail_model_prices(model,input_cost_per_token,output_cost_per_token,
           cache_read_input_token_cost,cache_creation_input_token_cost,source,catalog_model,source_label)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
"""


async def _one(
    connection: psycopg.AsyncConnection,
    query: str,
    params: Sequence[object],
    record_type: type[_Record],
) -> _Record | None:
    cursor: Final = await connection.execute(query, params)
    row: Final = await cursor.fetchone()
    return record_type.model_validate(row) if row is not None else None


async def _many(
    connection: psycopg.AsyncConnection,
    query: str,
    params: Sequence[object],
    record_type: type[_Record],
) -> tuple[_Record, ...]:
    cursor: Final = await connection.execute(query, params)
    rows: Final = await cursor.fetchall()
    return tuple(record_type.model_validate(row) for row in rows)


def _wallet_view(row: _WalletRow) -> dict[str, JsonValue]:
    return WalletView(
        user_id=row.user_id,
        balance=nanos_to_amount(row.balance_nanos),
        reserved=nanos_to_amount(row.reserved_nanos),
        available=nanos_to_amount(row.balance_nanos - row.reserved_nanos),
        total_credited=nanos_to_amount(row.total_credited_nanos),
        total_spent=nanos_to_amount(row.total_spent_nanos),
        pricing_plan=PricePlan(
            id=row.plan_id,
            name=row.plan_name,
            multiplier=format(row.multiplier, "f"),
            discount=format(row.multiplier, "f"),
        ),
        discount=format(row.discount_override if row.discount_override is not None else row.multiplier, "f"),
        discount_override=format(row.discount_override, "f") if row.discount_override is not None else None,
        discount_source="customer" if row.discount_override is not None else "plan",
        pending_requests=row.pending_requests,
    ).model_dump(mode="json")


def _reservation_view(row: _ReservationRow) -> dict[str, JsonValue]:
    return ReservationView(
        request_id=row.request_id,
        user_id=row.user_id,
        model=row.model,
        reserved_amount=nanos_to_amount(row.reserved_nanos),
        amount=nanos_to_amount(row.settled_nanos) if row.settled_nanos is not None else None,
        status=row.status,
        price_snapshot=row.price_snapshot,
        details=row.details,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    ).model_dump(mode="json")


def _ledger_view(row: _LedgerRow) -> dict[str, JsonValue]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "kind": row.kind,
        "amount": nanos_to_amount(row.amount_nanos),
        "balance_after": nanos_to_amount(row.balance_after_nanos),
        "request_id": row.request_id,
        "model": row.model,
        "created_at": row.created_at.isoformat(),
        "description": row.description,
        "reference": row.reference,
        "actor_id": row.actor_id,
        "details": row.details,
    }


def _pagination(limit: int, offset: int) -> None:
    if limit < 1 or limit > 100 or offset < 0:
        raise ValueError("Pagination requires 1 <= limit <= 100 and a non-negative offset")


def _manual_resolution(details: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    if "idempotency_key" not in details:
        return None
    return {key: details.get(key) for key in ("actor_id", "reference", "reason", "idempotency_key")}


def _model_price_view(row: _ModelPriceRow) -> dict[str, JsonValue]:
    return {
        "model": row.model,
        "input_cost_per_token": format(row.input_cost_per_token, "f"),
        "output_cost_per_token": format(row.output_cost_per_token, "f"),
        "cache_read_input_token_cost": format(row.cache_read_input_token_cost, "f"),
        "cache_creation_input_token_cost": format(row.cache_creation_input_token_cost, "f"),
        "version": row.version,
        "updated_at": row.updated_at.isoformat(),
        "source": row.source,
        "catalog_model": row.catalog_model,
        "source_label": row.source_label,
    }


def _model_price_rates(rates: Mapping[str, JsonValue]) -> tuple[Decimal, ...]:
    try:
        parsed: Final = tuple(Decimal(str(rates[key])) for key in _MODEL_PRICE_KEYS)
        if any(
            not rate.is_finite()
            or rate < 0
            or rate > 1_000_000
            or rate.quantize(Decimal("0.000000000000000001")) != rate
            for rate in parsed
        ):
            raise InvalidAmount("Token prices must be finite non-negative values with at most eighteen decimals")
        return parsed
    except (InvalidOperation, ValueError, TypeError, KeyError) as exc:
        raise InvalidAmount("All four token prices are required as decimal USD values") from exc


def model_discount_view(
    model: str,
    *,
    customer_discount: Decimal | None = None,
    plan_discount: Decimal | None = None,
    legacy_customer: Decimal | None = None,
    legacy_plan: Decimal = Decimal("1"),
) -> dict[str, JsonValue]:
    factor, source = _model_discount_factor(customer_discount, plan_discount, legacy_customer, legacy_plan)
    return {
        "model": model,
        "discount": format(factor, "f"),
        "discount_source": source,
        "customer_discount": format(customer_discount, "f") if customer_discount is not None else None,
        "plan_discount": format(plan_discount, "f") if plan_discount is not None else None,
    }


def _model_discount_factor(
    customer: Decimal | None,
    plan: Decimal | None,
    legacy_customer: Decimal | None,
    legacy_plan: Decimal,
) -> tuple[Decimal, str]:
    if customer is not None:
        return customer, "customer_model"
    if plan is not None:
        return plan, "plan_model"
    if legacy_customer is not None:
        return legacy_customer, "legacy_customer" if legacy_customer != 1 else "standard"
    if legacy_plan != 1:
        return legacy_plan, "legacy_plan"
    return Decimal("1"), "standard"


def _discount_models(models: Sequence[str]) -> tuple[str, ...]:
    if not models or len(models) > 500 or any(not model.strip() or len(model) > 191 for model in models):
        raise ValueError("Select between 1 and 500 public model names of at most 191 characters")
    return tuple(dict.fromkeys(models))


class WalletStore:
    def __init__(self, database_url: str, max_connections: int = 16) -> None:
        self.database_url = database_url
        self._slots = asyncio.Semaphore(max_connections)
        self._initialization = asyncio.Lock()
        self._initialized = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._slots:
            async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as connection:
                async with connection.transaction():
                    yield connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization:
            if self._initialized:
                return
            schema: Final = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            async with self.transaction() as connection:
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('retail:schema', 0))")
                await connection.execute(schema, prepare=False)
            self._initialized = True

    async def get_wallet(self, user_id: str, create: bool = False) -> dict[str, JsonValue]:
        async with self.transaction() as connection:
            if create:
                await connection.execute(
                    "INSERT INTO retail_wallets(user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,)
                )
            row: Final = await _one(connection, _WALLET_SELECT + " WHERE w.user_id=%s", (user_id,), _WalletRow)
            if row is None:
                raise WalletNotFound("Wallet is not enabled for this user")
            return _wallet_view(row)

    async def list_wallets(self, query: str = "", limit: int = 25, offset: int = 0) -> dict[str, JsonValue]:
        _pagination(limit, offset)
        pattern: Final = "%" + query + "%"
        async with self.transaction() as connection:
            total: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_wallets WHERE user_id ILIKE %s", (pattern,), _CountRow
            )
            rows: Final = await _many(
                connection,
                _WALLET_SELECT + " WHERE w.user_id ILIKE %s ORDER BY w.created_at DESC LIMIT %s OFFSET %s",
                (pattern, limit, offset),
                _WalletRow,
            )
            return {
                "items": [_wallet_view(row) for row in rows],
                "total": total.total if total is not None else 0,
                "limit": limit,
                "offset": offset,
            }

    async def ledger(
        self, user_id: str, limit: int = 25, offset: int = 0, kind: str | None = None
    ) -> dict[str, JsonValue]:
        _pagination(limit, offset)
        if kind not in (None, "credit", "charge", "release"):
            raise ValueError("Unknown ledger type")
        clause: Final = " WHERE user_id=%s" + (" AND kind=%s" if kind is not None else "")
        params: Final = (user_id, kind) if kind is not None else (user_id,)
        async with self.transaction() as connection:
            total: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_ledger" + clause, params, _CountRow
            )
            rows: Final = await _many(
                connection,
                _LEDGER_SELECT + clause + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
                _LedgerRow,
            )
            return {
                "items": [_ledger_view(row) for row in rows],
                "total": total.total if total is not None else 0,
                "limit": limit,
                "offset": offset,
            }

    async def credit(
        self,
        user_id: str,
        amount: str,
        reference: str,
        description: str,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, JsonValue]:
        nanos: Final = amount_to_nanos(amount, allow_zero=False)
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("A non-empty idempotency key of at most 200 characters is required")
        payment_reference: Final = reference.strip()
        if not payment_reference or len(payment_reference) > 191:
            raise ValueError("A non-empty payment reference of at most 191 characters is required")
        operation: Final = "credit:" + idempotency_key
        payload: Final = {
            "user_id": user_id,
            "amount": nanos_to_amount(nanos),
            "reference": payment_reference,
            "description": description,
            "actor_id": actor_id,
        }
        async with self.transaction() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("credit:reference:" + payment_reference,)
            )
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (operation,))
            alias: Final = await _one(
                connection,
                "SELECT payload FROM retail_credit_idempotency WHERE operation_key=%s",
                (operation,),
                _CreditOperationRow,
            )
            if alias is not None and alias.payload != payload:
                raise IdempotencyConflict("Idempotency key has already been used for a different credit")
            previous: Final = await _one(
                connection, _LEDGER_SELECT + " WHERE operation_key=%s", (operation,), _LedgerRow
            )
            if previous is not None and previous.payload != payload:
                raise IdempotencyConflict("Idempotency key has already been used for a different credit")
            payment: Final = await _one(
                connection,
                _LEDGER_SELECT + " WHERE kind='credit' AND reference=%s",
                (payment_reference,),
                _LedgerRow,
            )
            if payment is not None and payment.payload != payload:
                raise IdempotencyConflict("This payment reference has already been used for a different credit")
            wallet: Final = await self._lock_wallet(connection, user_id)
            if previous is not None or payment is not None:
                original: Final = previous if previous is not None else payment
                if original is not None:
                    await connection.execute(
                        """INSERT INTO retail_credit_idempotency(operation_key,ledger_id,payload)
                           VALUES (%s,%s,%s) ON CONFLICT(operation_key) DO NOTHING""",
                        (operation, original.id, Jsonb(payload)),
                    )
                return _wallet_view(wallet)
            updated: Final = await _one(
                connection,
                """UPDATE retail_wallets SET balance_nanos=balance_nanos+%s,
                       total_credited_nanos=total_credited_nanos+%s,updated_at=now()
                   WHERE user_id=%s RETURNING user_id""",
                (nanos, nanos, user_id),
                _UserIdRow,
            )
            if updated is None:
                raise WalletNotFound("Wallet is not enabled for this user")
            ledger_id: Final = uuid4()
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,
                       operation_key,payload,reference,description,actor_id)
                   VALUES (%s,%s,'credit',%s,%s,%s,%s,%s,%s,%s)""",
                (
                    ledger_id,
                    user_id,
                    nanos,
                    wallet.balance_nanos + nanos,
                    operation,
                    Jsonb(payload),
                    payment_reference,
                    description,
                    actor_id,
                ),
            )
            await connection.execute(
                "INSERT INTO retail_credit_idempotency(operation_key,ledger_id,payload) VALUES (%s,%s,%s)",
                (operation, ledger_id, Jsonb(payload)),
            )
            current: Final = await self._read_wallet(connection, user_id)
            return _wallet_view(current)

    async def reserve(
        self,
        user_id: str,
        request_id: str,
        model: str,
        amount: str,
        price_snapshot: Mapping[str, JsonValue],
        max_parallel: int = 10,
    ) -> dict[str, JsonValue]:
        nanos: Final = cost_to_nanos(amount)
        if not request_id or max_parallel < 1:
            raise ValueError("A request ID and a positive parallel request limit are required")
        snapshot: Final = dict(price_snapshot)
        async with self.transaction() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("reserve:" + request_id,)
            )
            wallet: Final = await self._lock_wallet(connection, user_id)
            previous: Final = await _one(
                connection, "SELECT * FROM retail_reservations WHERE request_id=%s", (request_id,), _ReservationRow
            )
            if previous is not None:
                if (previous.user_id, previous.model, previous.reserved_nanos, previous.price_snapshot) != (
                    user_id,
                    model,
                    nanos,
                    snapshot,
                ):
                    raise IdempotencyConflict("Request ID has already been used with different reservation data")
                return _reservation_view(previous)
            if wallet.balance_nanos - wallet.reserved_nanos < nanos or wallet.balance_nanos <= 0:
                raise InsufficientFunds("Insufficient available wallet balance")
            if wallet.pending_requests >= max_parallel:
                raise ParallelLimitExceeded("The wallet's concurrent request limit has been reached")
            await connection.execute(
                "UPDATE retail_wallets SET reserved_nanos=reserved_nanos+%s,updated_at=now() WHERE user_id=%s",
                (nanos, user_id),
            )
            reservation: Final = await _one(
                connection,
                """INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (request_id, user_id, model, nanos, Jsonb(snapshot)),
                _ReservationRow,
            )
            if reservation is None:
                raise ReservationNotFound("The request reservation could not be created")
            return _reservation_view(reservation)

    async def settle(self, request_id: str, cost: str, details: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        nanos: Final = cost_to_nanos(cost)
        async with self.transaction() as connection:
            reservation: Final = await self._lock_reservation(connection, request_id)
            if reservation.status == "settled":
                if reservation.settled_nanos != nanos or _manual_resolution(details) != _manual_resolution(
                    reservation.details
                ):
                    raise IdempotencyConflict("This request has already settled with a different cost")
                return _reservation_view(reservation)
            if reservation.status == "released" and _manual_resolution(reservation.details) is not None:
                raise IdempotencyConflict("An administrator has already released this request")
            released: Final = reservation.reserved_nanos if reservation.status == "reserved" else 0
            wallet: Final = await self._read_wallet(connection, reservation.user_id)
            await connection.execute(
                """UPDATE retail_wallets SET balance_nanos=balance_nanos-%s,reserved_nanos=reserved_nanos-%s,
                       total_spent_nanos=total_spent_nanos+%s,updated_at=now() WHERE user_id=%s""",
                (nanos, released, nanos, reservation.user_id),
            )
            saved_details: Final = {**dict(details), "price_snapshot": reservation.price_snapshot}
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,operation_key,
                       request_id,model,description,details)
                   VALUES (%s,%s,'charge',%s,%s,%s,%s,%s,'API usage',%s)""",
                (
                    uuid4(),
                    reservation.user_id,
                    -nanos,
                    wallet.balance_nanos - nanos,
                    "charge:" + request_id,
                    request_id,
                    reservation.model,
                    Jsonb(saved_details),
                ),
            )
            settled: Final = await _one(
                connection,
                """UPDATE retail_reservations SET status='settled',settled_nanos=%s,details=%s,updated_at=now()
                   WHERE request_id=%s RETURNING *""",
                (nanos, Jsonb(saved_details), request_id),
                _ReservationRow,
            )
            if settled is None:
                raise ReservationNotFound("Request reservation does not exist")
            return _reservation_view(settled)

    async def release(
        self, request_id: str, reason: str, details: Mapping[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        async with self.transaction() as connection:
            reservation: Final = await self._lock_reservation(connection, request_id)
            if reservation.status != "reserved":
                if _manual_resolution(details or {}) is not None and (
                    reservation.status != "released"
                    or _manual_resolution(details or {}) != _manual_resolution(reservation.details)
                ):
                    raise IdempotencyConflict("This request already has a different final resolution")
                return _reservation_view(reservation)
            wallet: Final = await self._read_wallet(connection, reservation.user_id)
            await connection.execute(
                "UPDATE retail_wallets SET reserved_nanos=reserved_nanos-%s,updated_at=now() WHERE user_id=%s",
                (reservation.reserved_nanos, reservation.user_id),
            )
            saved_details: Final = {**dict(details or {}), "reason": reason}
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,operation_key,
                       request_id,model,description,details)
                   VALUES (%s,%s,'release',0,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    reservation.user_id,
                    wallet.balance_nanos,
                    "release:" + request_id,
                    request_id,
                    reservation.model,
                    reason,
                    Jsonb(saved_details),
                ),
            )
            released: Final = await _one(
                connection,
                """UPDATE retail_reservations SET status='released',reason=%s,details=%s,updated_at=now()
                   WHERE request_id=%s RETURNING *""",
                (reason, Jsonb(saved_details), request_id),
                _ReservationRow,
            )
            if released is None:
                raise ReservationNotFound("Request reservation does not exist")
            return _reservation_view(released)

    async def get_reservation(self, request_id: str) -> dict[str, JsonValue] | None:
        async with self.transaction() as connection:
            row: Final = await _one(
                connection, "SELECT * FROM retail_reservations WHERE request_id=%s", (request_id,), _ReservationRow
            )
            return _reservation_view(row) if row is not None else None

    async def list_reservations(
        self, user_id: str | None = None, state: str = "reserved", limit: int = 25, offset: int = 0
    ) -> dict[str, JsonValue]:
        _pagination(limit, offset)
        if state not in ("reserved", "settled", "released"):
            raise ValueError("Unknown reservation state")
        clause: Final = " WHERE status=%s" + (" AND user_id=%s" if user_id is not None else "")
        params: Final = (state, user_id) if user_id is not None else (state,)
        async with self.transaction() as connection:
            total: Final = await _one(
                connection, "SELECT count(*) AS total FROM retail_reservations" + clause, params, _CountRow
            )
            rows: Final = await _many(
                connection,
                "SELECT * FROM retail_reservations" + clause + " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
                _ReservationRow,
            )
            return {
                "items": [_reservation_view(row) for row in rows],
                "total": total.total if total is not None else 0,
                "limit": limit,
                "offset": offset,
            }

    async def list_plans(self) -> dict[str, JsonValue]:
        async with self.transaction() as connection:
            rows: Final = await _many(
                connection, "SELECT id,name,multiplier FROM retail_pricing_plans ORDER BY created_at,id", (), _PlanRow
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "multiplier": format(row.multiplier, "f"),
                        "discount": format(row.multiplier, "f"),
                    }
                    for row in rows
                ]
            }

    async def get_model_price(
        self, model: str, initial_snapshot: Mapping[str, JsonValue] | None = None
    ) -> dict[str, JsonValue] | None:
        async with self.transaction() as connection:
            if initial_snapshot is not None:
                rates: Final = _model_price_rates(initial_snapshot)
                await connection.execute(
                    _MODEL_PRICE_INSERT + " ON CONFLICT(model) DO NOTHING",
                    (
                        model,
                        *rates,
                        initial_snapshot.get("source", "legacy"),
                        initial_snapshot.get("catalog_model"),
                        initial_snapshot.get("source_label", "Existing reference, review required"),
                    ),
                )
            row: Final = await _one(
                connection, "SELECT * FROM retail_model_prices WHERE model=%s", (model,), _ModelPriceRow
            )
            return _model_price_view(row) if row is not None else None

    async def list_model_prices(self) -> dict[str, JsonValue]:
        async with self.transaction() as connection:
            rows: Final = await _many(
                connection, "SELECT * FROM retail_model_prices ORDER BY model", (), _ModelPriceRow
            )
            return {"items": [_model_price_view(row) for row in rows]}

    async def save_model_price(
        self,
        model: str,
        rates: Mapping[str, JsonValue],
        *,
        source: str = "manual",
        catalog_model: str | None = None,
        source_label: str = "Manual official reference",
    ) -> dict[str, JsonValue]:
        if not model or len(model) > 191:
            raise ValueError("A model name of at most 191 characters is required")
        parsed: Final = _model_price_rates(rates)
        async with self.transaction() as connection:
            return await self._save_model_price(
                connection,
                model,
                parsed,
                source,
                catalog_model,
                source_label,
            )

    async def save_reference_prices(self, cards: Sequence[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
        parsed: Final = tuple((card, _model_price_rates(card)) for card in cards)
        async with self.transaction() as connection:
            saved: Final = tuple(
                [
                    await self._save_model_price(
                        connection,
                        str(card["model"]),
                        rates,
                        "catalog",
                        str(card["catalog_model"]),
                        str(card["source_label"]),
                    )
                    for card, rates in parsed
                ]
            )
            return {"items": list(saved)}

    async def _save_model_price(
        self,
        connection: psycopg.AsyncConnection,
        model: str,
        rates: Sequence[Decimal],
        source: str,
        catalog_model: str | None,
        source_label: str,
    ) -> dict[str, JsonValue]:
        if not model or len(model) > 191 or source not in {"manual", "catalog"}:
            raise ValueError("A configured model name and reference source are required")
        row: Final = await _one(
            connection,
            _MODEL_PRICE_INSERT
            + """ ON CONFLICT(model) DO UPDATE SET
                input_cost_per_token=EXCLUDED.input_cost_per_token,
                output_cost_per_token=EXCLUDED.output_cost_per_token,
                cache_read_input_token_cost=EXCLUDED.cache_read_input_token_cost,
                cache_creation_input_token_cost=EXCLUDED.cache_creation_input_token_cost,
                source=EXCLUDED.source,catalog_model=EXCLUDED.catalog_model,source_label=EXCLUDED.source_label,
                version=retail_model_prices.version+1,updated_at=now()
                RETURNING *""",
            (model, *rates, source, catalog_model, source_label),
            _ModelPriceRow,
        )
        if row is None:
            raise ValueError("The model reference price could not be saved")
        return _model_price_view(row)

    async def save_plan(self, plan_id: str, name: str, multiplier: str | None = None) -> dict[str, JsonValue]:
        value: Final = parse_discount(multiplier) if multiplier is not None else None
        if not plan_id or len(plan_id) > 100 or not name or len(name) > 200:
            raise ValueError("A pricing plan ID and name are required")
        async with self.transaction() as connection:
            row: Final = await _one(
                connection,
                """INSERT INTO retail_pricing_plans(id,name,multiplier) VALUES (%s,%s,COALESCE(%s,1))
                   ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,
                       multiplier=COALESCE(%s,retail_pricing_plans.multiplier),updated_at=now()
                   RETURNING id,name,multiplier""",
                (plan_id, name, value, value),
                _PlanRow,
            )
            if row is None:
                raise PlanNotFound("Pricing plan could not be saved")
            return {
                "id": row.id,
                "name": row.name,
                "multiplier": format(row.multiplier, "f"),
                "discount": format(row.multiplier, "f"),
            }

    async def set_discount(self, user_id: str, discount: str | None) -> dict[str, JsonValue]:
        value: Final = parse_discount(discount) if discount is not None else None
        async with self.transaction() as connection:
            await self._lock_wallet(connection, user_id)
            await connection.execute(
                "UPDATE retail_wallets SET discount_override=%s,updated_at=now() WHERE user_id=%s", (value, user_id)
            )
            return _wallet_view(await self._read_wallet(connection, user_id))

    async def get_model_discount(self, user_id: str, model: str, pool_id: str | None = None) -> dict[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import get_pool_model_discount

            return await get_pool_model_discount(self, user_id, model, pool_id)
        async with self.transaction() as connection:
            row: Final = await _one(
                connection,
                """SELECT w.user_id,w.plan_id,w.discount_override AS legacy_customer,p.multiplier AS legacy_plan,
                    c.discount AS customer_discount,d.discount AS plan_discount
                    FROM retail_wallets w JOIN retail_pricing_plans p ON p.id=w.plan_id
                    LEFT JOIN retail_customer_model_discounts c ON c.user_id=w.user_id AND c.model=%s
                    LEFT JOIN retail_plan_model_discounts d ON d.plan_id=w.plan_id AND d.model=%s
                    WHERE w.user_id=%s""",
                (model, model, user_id),
                _ModelDiscountFactors,
            )
            if row is None:
                raise WalletNotFound("Wallet is not enabled for this user")
            return {
                "user_id": row.user_id,
                "plan_id": row.plan_id,
                **model_discount_view(
                    model,
                    customer_discount=row.customer_discount,
                    plan_discount=row.plan_discount,
                    legacy_customer=row.legacy_customer,
                    legacy_plan=row.legacy_plan,
                ),
            }

    async def list_model_discounts(
        self,
        user_id: str,
        models: Sequence[str] = (),
        pool_id: str | None = None,
    ) -> dict[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import list_pool_model_discounts

            return await list_pool_model_discounts(self, pool_id, models, user_id=user_id)
        async with self.transaction() as connection:
            wallet: Final = await self._read_wallet(connection, user_id)
            customer_rows: Final = await _many(
                connection,
                "SELECT model,discount FROM retail_customer_model_discounts WHERE user_id=%s",
                (user_id,),
                _ModelDiscountRow,
            )
            plan_rows: Final = await _many(
                connection,
                "SELECT model,discount FROM retail_plan_model_discounts WHERE plan_id=%s",
                (wallet.plan_id,),
                _ModelDiscountRow,
            )
            customer: Final = {row.model: row.discount for row in customer_rows}
            plan: Final = {row.model: row.discount for row in plan_rows}
            names: Final = sorted(frozenset(models) | customer.keys() | plan.keys())
            return {
                "user_id": user_id,
                "plan_id": wallet.plan_id,
                "items": [
                    model_discount_view(
                        model,
                        customer_discount=customer.get(model),
                        plan_discount=plan.get(model),
                        legacy_customer=wallet.discount_override,
                        legacy_plan=wallet.multiplier,
                    )
                    for model in names
                ],
            }

    async def list_plan_model_discounts(
        self,
        plan_id: str,
        models: Sequence[str] = (),
        pool_id: str | None = None,
    ) -> dict[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import list_pool_model_discounts

            return await list_pool_model_discounts(self, pool_id, models, plan_id=plan_id)
        async with self.transaction() as connection:
            plan: Final = await _one(
                connection,
                "SELECT id,name,multiplier FROM retail_pricing_plans WHERE id=%s",
                (plan_id,),
                _PlanRow,
            )
            if plan is None:
                raise PlanNotFound("Pricing plan does not exist")
            rows: Final = await _many(
                connection,
                "SELECT model,discount FROM retail_plan_model_discounts WHERE plan_id=%s",
                (plan_id,),
                _ModelDiscountRow,
            )
            discounts: Final = {row.model: row.discount for row in rows}
            return {
                "plan_id": plan_id,
                "items": [
                    model_discount_view(model, plan_discount=discounts.get(model), legacy_plan=plan.multiplier)
                    for model in sorted(frozenset(models) | discounts.keys())
                ],
            }

    async def set_model_discounts(
        self,
        user_id: str,
        models: Sequence[str],
        discount: str | None,
        pool_id: str | None = None,
    ) -> dict[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import set_pool_model_discounts

            return await set_pool_model_discounts(self, pool_id, models, discount, user_id=user_id)
        names: Final = _discount_models(models)
        value: Final = parse_discount(discount) if discount is not None else None
        async with self.transaction() as connection:
            await self._lock_wallet(connection, user_id)
            if value is None:
                await connection.execute(
                    "DELETE FROM retail_customer_model_discounts WHERE user_id=%s AND model=ANY(%s::text[])",
                    (user_id, list(names)),
                )
            else:
                await connection.execute(
                    """INSERT INTO retail_customer_model_discounts(user_id,model,discount)
                    SELECT %s,model,%s FROM unnest(%s::text[]) AS selected(model)
                    ON CONFLICT(user_id,model) DO UPDATE SET discount=EXCLUDED.discount,updated_at=now()""",
                    (user_id, value, list(names)),
                )
        return await self.list_model_discounts(user_id)

    async def set_plan_model_discounts(
        self,
        plan_id: str,
        models: Sequence[str],
        discount: str | None,
        pool_id: str | None = None,
    ) -> dict[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import set_pool_model_discounts

            return await set_pool_model_discounts(self, pool_id, models, discount, plan_id=plan_id)
        names: Final = _discount_models(models)
        value: Final = parse_discount(discount) if discount is not None else None
        async with self.transaction() as connection:
            plan: Final = await _one(
                connection,
                "SELECT id,name,multiplier FROM retail_pricing_plans WHERE id=%s FOR UPDATE",
                (plan_id,),
                _PlanRow,
            )
            if plan is None:
                raise PlanNotFound("Pricing plan does not exist")
            if value is None:
                await connection.execute(
                    "DELETE FROM retail_plan_model_discounts WHERE plan_id=%s AND model=ANY(%s::text[])",
                    (plan_id, list(names)),
                )
            else:
                await connection.execute(
                    """INSERT INTO retail_plan_model_discounts(plan_id,model,discount)
                    SELECT %s,model,%s FROM unnest(%s::text[]) AS selected(model)
                    ON CONFLICT(plan_id,model) DO UPDATE SET discount=EXCLUDED.discount,updated_at=now()""",
                    (plan_id, value, list(names)),
                )
        return await self.list_plan_model_discounts(plan_id)

    async def set_plan(self, user_id: str, plan_id: str) -> dict[str, JsonValue]:
        async with self.transaction() as connection:
            await self._lock_wallet(connection, user_id)
            plan: Final = await _one(
                connection, "SELECT id,name,multiplier FROM retail_pricing_plans WHERE id=%s", (plan_id,), _PlanRow
            )
            if plan is None:
                raise PlanNotFound("Pricing plan does not exist")
            await connection.execute(
                "UPDATE retail_wallets SET plan_id=%s,updated_at=now() WHERE user_id=%s", (plan_id, user_id)
            )
            wallet: Final = await self._read_wallet(connection, user_id)
            return _wallet_view(wallet)

    async def _read_wallet(self, connection: psycopg.AsyncConnection, user_id: str) -> _WalletRow:
        row: Final = await _one(connection, _WALLET_SELECT + " WHERE w.user_id=%s", (user_id,), _WalletRow)
        if row is None:
            raise WalletNotFound("Wallet is not enabled for this user")
        return row

    async def _lock_wallet(self, connection: psycopg.AsyncConnection, user_id: str) -> _WalletRow:
        row: Final = await _one(
            connection, "SELECT user_id FROM retail_wallets WHERE user_id=%s FOR UPDATE", (user_id,), _UserIdRow
        )
        if row is None:
            raise WalletNotFound("Wallet is not enabled for this user")
        return await self._read_wallet(connection, user_id)

    async def _lock_reservation(self, connection: psycopg.AsyncConnection, request_id: str) -> _ReservationRow:
        owner: Final = await _one(
            connection, "SELECT user_id FROM retail_reservations WHERE request_id=%s", (request_id,), _UserIdRow
        )
        if owner is None:
            raise ReservationNotFound("Request reservation does not exist")
        await self._lock_wallet(connection, owner.user_id)
        row: Final = await _one(
            connection,
            "SELECT * FROM retail_reservations WHERE request_id=%s FOR UPDATE",
            (request_id,),
            _ReservationRow,
        )
        if row is None:
            raise ReservationNotFound("Request reservation does not exist")
        return row


@lru_cache(maxsize=1)
def get_store() -> WalletStore:
    return WalletStore(os.environ.get("RETAIL_DATABASE_URL") or os.environ["DATABASE_URL"])

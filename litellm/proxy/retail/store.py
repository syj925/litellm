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
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .database import RetailDatabase, database_integer_setting, database_setting
from .expression_pricing import ExpressionPricingError, validate_expression_policy
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
from .pricing import validated_cache_write_tiers


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
    cache_write_tiers: dict[str, str] = Field(default_factory=dict)
    cache_write_price_published: bool | None = None
    expression_policy: dict[str, JsonValue] | None = None
    version: int
    updated_at: datetime
    source: str = "legacy"
    catalog_model: str | None = None
    source_label: str = "Existing reference, review required"


class _UserIdRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str


class _BalanceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    balance_nanos: int


_Record = TypeVar("_Record", bound=BaseModel)
_WALLET_SELECT: Final = """
    SELECT w.*, p.name AS plan_name, p.multiplier,
           (SELECT count(*) FROM retail_reservations r
            WHERE r.user_id=w.user_id AND r.status='reserved' AND r.active_until>now()) AS pending_requests
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
           cache_read_input_token_cost,cache_creation_input_token_cost,source,catalog_model,source_label,cache_write_tiers,
           cache_write_price_published,expression_policy)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        "cache_write_tiers": {key: value for key, value in row.cache_write_tiers.items()},
        "cache_write_price_published": row.cache_write_price_published,
        "expression_policy": row.expression_policy,
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


def _model_price_tiers(rates: Mapping[str, JsonValue]) -> dict[str, str]:
    try:
        return validated_cache_write_tiers(rates.get("cache_write_tiers", {}))
    except ValueError as exc:
        raise InvalidAmount(str(exc)) from exc


def _model_price_published(rates: Mapping[str, JsonValue], source: str) -> bool | None:
    value: Final = rates.get("cache_write_price_published")
    if value is not None and not isinstance(value, bool):
        raise InvalidAmount("Published cache write pricing must be a boolean or unknown")
    return True if source == "manual" and value is None else value


def _model_expression_policy(rates: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    raw: Final = rates.get("expression_policy")
    if raw is None:
        return None
    try:
        return validate_expression_policy(raw).model_dump(mode="json")
    except ExpressionPricingError as exc:
        raise InvalidAmount(str(exc)) from exc


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
    def __init__(
        self, database_url: str, max_connections: int = 16, *, diagnostic_connections: int = 3,
        borrow_timeout: float = 5.0, max_waiting: int = 64, diagnostic_max_waiting: int = 12,
        account_timeout: float = 5.0, account_max_waiting: int = 64, max_account_gates: int = 1024,
        active_lease_seconds: int = 120,
        critical_connections: int = 4, critical_max_waiting: int = 544, critical_borrow_timeout: float = 10,
        statement_timeout_ms: int = 5000, lock_timeout_ms: int = 1000, idle_transaction_timeout_ms: int = 10000,
    ) -> None:
        if active_lease_seconds < 30:
            raise ValueError("Active request leases must last at least 30 seconds")
        self.database_url = database_url
        self._active_lease_seconds = active_lease_seconds
        self._database = RetailDatabase(
            database_url, finance_connections=max_connections, diagnostic_connections=diagnostic_connections,
            borrow_timeout=borrow_timeout, max_waiting=max_waiting, diagnostic_max_waiting=diagnostic_max_waiting,
            account_timeout=account_timeout, account_max_waiting=account_max_waiting, max_account_gates=max_account_gates,
            statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms,
            idle_transaction_timeout_ms=idle_transaction_timeout_ms,
            critical_connections=critical_connections, critical_max_waiting=critical_max_waiting,
            critical_borrow_timeout=critical_borrow_timeout,
        )
        self._initialization = asyncio.Lock()
        self._initialized = False
        self._control_plane_initialization = asyncio.Lock()
        self._control_plane_initialized = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._database.transaction() as connection:
            yield connection

    @asynccontextmanager
    async def diagnostic_transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._database.transaction(diagnostic=True) as connection:
            yield connection

    @asynccontextmanager
    async def critical_transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._database.transaction(critical=True) as connection:
            yield connection

    @asynccontextmanager
    async def account_transaction(self, user_id: str) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._database.account(user_id):
            async with self.transaction() as connection:
                yield connection

    @asynccontextmanager
    async def _reservation_transaction(self, request_id: str) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.transaction() as lookup:
            owner: Final = await _one(
                lookup, "SELECT user_id FROM retail_reservations WHERE request_id=%s", (request_id,), _UserIdRow
            )
        if owner is None:
            raise ReservationNotFound("Request reservation does not exist")
        async with self.account_transaction(owner.user_id) as connection:
            await self._lock_wallet_identity(connection, owner.user_id)
            yield connection

    async def aclose(self) -> None:
        await self._database.aclose()

    @property
    def account_gate_count(self) -> int:
        return self._database.account_gate_count

    def pool_stats(self) -> Mapping[str, Mapping[str, int]]:
        return self._database.pool_stats()

    async def initialize(self) -> None:
        await self._database.open()
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

    async def initialize_control_plane(self) -> None:
        from .control_plane_flags import operation_audit_enabled, organizations_enabled
        from .operation_events import initialize_schema as initialize_audit
        from .organization_store import initialize_schema as initialize_organizations

        if self._control_plane_initialized or not operation_audit_enabled():
            return
        async with self._control_plane_initialization:
            if self._control_plane_initialized:
                return
            async with self.diagnostic_transaction() as connection:
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('retail:control-plane-schema',0))")
                await initialize_audit(connection)
                if organizations_enabled():
                    await initialize_organizations(connection)
            self._control_plane_initialized = True

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
        async with self.account_transaction(user_id) as connection:
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
        async with self.account_transaction(user_id) as connection:
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
            reservation: Final = await _one(
                connection,
                """WITH held AS (
                     UPDATE retail_wallets SET reserved_nanos=reserved_nanos+%s,updated_at=now()
                     WHERE user_id=%s RETURNING user_id
                   ) INSERT INTO retail_reservations(request_id,user_id,model,reserved_nanos,price_snapshot,active_until)
                   SELECT %s,held.user_id,%s,%s,%s,now()+(%s * interval '1 second') FROM held RETURNING *""",
                (nanos, user_id, request_id, model, nanos, Jsonb(snapshot), self._active_lease_seconds),
                _ReservationRow,
            )
            if reservation is None:
                raise ReservationNotFound("The request reservation could not be created")
            return _reservation_view(reservation)

    async def record_pending_usage(self, request_id: str, details: Mapping[str, JsonValue]) -> None:
        async with self._reservation_transaction(request_id) as connection:
            reservation: Final = await self._lock_reservation(connection, request_id)
            if reservation.status != "reserved":
                return
            saved: Final = {**reservation.details, **details, "price_snapshot": reservation.price_snapshot}
            await connection.execute(
                "UPDATE retail_reservations SET details=%s,updated_at=now() WHERE request_id=%s AND status='reserved'",
                (Jsonb(saved), request_id),
            )

    async def settle(self, request_id: str, cost: str, details: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        nanos: Final = cost_to_nanos(cost)
        async with self._reservation_transaction(request_id) as connection:
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
            balance: Final = await _one(
                connection,
                """UPDATE retail_wallets SET balance_nanos=balance_nanos-%s,reserved_nanos=reserved_nanos-%s,
                       total_spent_nanos=total_spent_nanos+%s,updated_at=now() WHERE user_id=%s
                       RETURNING balance_nanos""",
                (nanos, released, nanos, reservation.user_id),
                _BalanceRow,
            )
            if balance is None:
                raise WalletNotFound("Wallet is not enabled for this user")
            saved_details: Final = {**dict(details), "price_snapshot": reservation.price_snapshot}
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,operation_key,
                       request_id,model,description,details)
                   VALUES (%s,%s,'charge',%s,%s,%s,%s,%s,'API usage',%s)""",
                (
                    uuid4(),
                    reservation.user_id,
                    -nanos,
                    balance.balance_nanos,
                    "charge:" + request_id,
                    request_id,
                    reservation.model,
                    Jsonb(saved_details),
                ),
            )
            settled: Final = await _one(
                connection,
                """UPDATE retail_reservations SET status='settled',settled_nanos=%s,details=%s,active_until=NULL,updated_at=now()
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
        async with self._reservation_transaction(request_id) as connection:
            reservation: Final = await self._lock_reservation(connection, request_id)
            if reservation.status != "reserved":
                if _manual_resolution(details or {}) is not None and (
                    reservation.status != "released"
                    or _manual_resolution(details or {}) != _manual_resolution(reservation.details)
                ):
                    raise IdempotencyConflict("This request already has a different final resolution")
                return _reservation_view(reservation)
            balance: Final = await _one(
                connection,
                """UPDATE retail_wallets SET reserved_nanos=reserved_nanos-%s,updated_at=now() WHERE user_id=%s
                   RETURNING balance_nanos""",
                (reservation.reserved_nanos, reservation.user_id),
                _BalanceRow,
            )
            if balance is None:
                raise WalletNotFound("Wallet is not enabled for this user")
            saved_details: Final = {**dict(details or {}), "reason": reason}
            await connection.execute(
                """INSERT INTO retail_ledger(id,user_id,kind,amount_nanos,balance_after_nanos,operation_key,
                       request_id,model,description,details)
                   VALUES (%s,%s,'release',0,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    reservation.user_id,
                    balance.balance_nanos,
                    "release:" + request_id,
                    request_id,
                    reservation.model,
                    reason,
                    Jsonb(saved_details),
                ),
            )
            released: Final = await _one(
                connection,
                """UPDATE retail_reservations SET status='released',reason=%s,details=%s,active_until=NULL,updated_at=now()
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
                        Jsonb(_model_price_tiers(initial_snapshot)),
                        _model_price_published(initial_snapshot, str(initial_snapshot.get("source", "legacy"))),
                        Jsonb(_model_expression_policy(initial_snapshot))
                        if initial_snapshot.get("expression_policy") is not None else None,
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
        source_label: str = "Custom reference price",
    ) -> dict[str, JsonValue]:
        if not model or len(model) > 191:
            raise ValueError("A model name of at most 191 characters is required")
        parsed: Final = _model_price_rates(rates)
        expression_policy: Final = _model_expression_policy(rates)
        async with self.transaction() as connection:
            return await self._save_model_price(
                connection,
                model,
                parsed,
                source,
                catalog_model,
                source_label,
                _model_price_tiers(rates),
                _model_price_published(rates, source),
                expression_policy,
            )

    async def save_reference_prices(self, cards: Sequence[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
        parsed: Final = tuple((card, _model_price_rates(card), _model_expression_policy(card)) for card in cards)
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
                        _model_price_tiers(card),
                        _model_price_published(card, "catalog"),
                        expression_policy,
                    )
                    for card, rates, expression_policy in parsed
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
        cache_write_tiers: Mapping[str, str],
        cache_write_price_published: bool | None,
        expression_policy: Mapping[str, JsonValue] | None,
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
                cache_write_tiers=EXCLUDED.cache_write_tiers,
                cache_write_price_published=EXCLUDED.cache_write_price_published,
                expression_policy=EXCLUDED.expression_policy,
                source=EXCLUDED.source,catalog_model=EXCLUDED.catalog_model,source_label=EXCLUDED.source_label,
                version=retail_model_prices.version+1,updated_at=now()
                RETURNING *""",
            (
                model, *rates, source, catalog_model, source_label, Jsonb(dict(cache_write_tiers)),
                cache_write_price_published, Jsonb(dict(expression_policy)) if expression_policy is not None else None,
            ),
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

    async def get_billing_context(
        self, user_id: str, model: str, pool_id: str | None = None,
    ) -> Mapping[str, JsonValue]:
        async with self.transaction() as connection:
            discount: Final = await self._billing_discount(connection, user_id, model, pool_id)
            price: Final = await _one(
                connection, "SELECT * FROM retail_model_prices WHERE model=%s", (model,), _ModelPriceRow
            )
            return {
                "price": _model_price_view(price) if price is not None else None,
                "discount": dict(discount),
            }

    async def _billing_discount(
        self, connection: psycopg.AsyncConnection, user_id: str, model: str, pool_id: str | None,
    ) -> Mapping[str, JsonValue]:
        try:
            return await self._read_model_discount(connection, user_id, model, pool_id)
        except WalletNotFound:
            await connection.execute(
                "INSERT INTO retail_wallets(user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,)
            )
            return await self._read_model_discount(connection, user_id, model, pool_id)

    async def get_model_discount(self, user_id: str, model: str, pool_id: str | None = None) -> Mapping[str, JsonValue]:
        async with self.transaction() as connection:
            return await self._read_model_discount(connection, user_id, model, pool_id)

    async def _read_model_discount(
        self, connection: psycopg.AsyncConnection, user_id: str, model: str, pool_id: str | None,
    ) -> Mapping[str, JsonValue]:
        if pool_id is not None:
            from .pool_pricing import _factors, pool_discount_view

            return pool_discount_view(model, await _factors(connection, pool_id, user_id=user_id, model=model))
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

    async def _lock_wallet_identity(self, connection: psycopg.AsyncConnection, user_id: str) -> None:
        row: Final = await _one(
            connection, "SELECT user_id FROM retail_wallets WHERE user_id=%s FOR UPDATE", (user_id,), _UserIdRow
        )
        if row is None:
            raise WalletNotFound("Wallet is not enabled for this user")

    async def _lock_wallet(self, connection: psycopg.AsyncConnection, user_id: str) -> _WalletRow:
        await self._lock_wallet_identity(connection, user_id)
        return await self._read_wallet(connection, user_id)

    async def _lock_reservation(self, connection: psycopg.AsyncConnection, request_id: str) -> _ReservationRow:
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
    inflight: Final = database_integer_setting("RETAIL_MAX_INFLIGHT", 512, 1, 100000)
    return WalletStore(
        os.environ.get("RETAIL_DATABASE_URL") or os.environ["DATABASE_URL"],
        max_connections=database_integer_setting("RETAIL_DB_MAX_CONNECTIONS", 16, 1, 128),
        diagnostic_connections=database_integer_setting("RETAIL_DB_DIAGNOSTIC_CONNECTIONS", 3, 1, 8),
        critical_connections=database_integer_setting("RETAIL_DB_CRITICAL_CONNECTIONS", 4, 1, 16),
        critical_max_waiting=database_integer_setting("RETAIL_DB_CRITICAL_MAX_WAITING", inflight + 32, inflight, 131072),
        critical_borrow_timeout=database_setting("RETAIL_DB_CRITICAL_BORROW_TIMEOUT_SECONDS", 10, 0.1, 60),
        borrow_timeout=database_setting("RETAIL_DB_BORROW_TIMEOUT_SECONDS", 5, 0.05, 60),
        max_waiting=database_integer_setting("RETAIL_DB_MAX_WAITING", 64, 1, 4096),
        diagnostic_max_waiting=database_integer_setting("RETAIL_DB_DIAGNOSTIC_MAX_WAITING", 12, 1, 128),
        account_timeout=database_setting("RETAIL_DB_ACCOUNT_TIMEOUT_SECONDS", 5, 0.05, 60),
        account_max_waiting=database_integer_setting("RETAIL_DB_ACCOUNT_MAX_WAITING", 64, 1, 1024),
        max_account_gates=database_integer_setting("RETAIL_DB_ACCOUNT_GATES", 1024, 1, 16384),
        active_lease_seconds=database_integer_setting("RETAIL_ACTIVE_LEASE_SECONDS", 120, 30, 3600),
        statement_timeout_ms=database_integer_setting("RETAIL_DB_STATEMENT_TIMEOUT_MS", 5000, 100, 600000),
        lock_timeout_ms=database_integer_setting("RETAIL_DB_LOCK_TIMEOUT_MS", 1000, 50, 60000),
        idle_transaction_timeout_ms=database_integer_setting("RETAIL_DB_IDLE_TRANSACTION_TIMEOUT_MS", 10000, 100, 600000),
    )

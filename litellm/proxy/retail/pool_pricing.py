from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import psycopg
from pydantic import BaseModel, ConfigDict, JsonValue

from .models import PlanNotFound, WalletNotFound, parse_discount
from .store import WalletStore, _discount_models, _many, _one


class _PoolFactors(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str | None
    plan_id: str
    pool_id: str
    pool_name: str
    pool_default_discount: Decimal
    customer_discount: Decimal | None
    plan_discount: Decimal | None
    customer_pool_discount: Decimal | None
    plan_pool_discount: Decimal | None


class _DiscountRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    discount: Decimal


class _ModelName(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str


def pool_discount_view(model: str, factors: _PoolFactors) -> dict[str, JsonValue]:
    options: Final = (
        (factors.customer_discount, "customer_pool_model"),
        (factors.plan_discount, "plan_pool_model"),
        (factors.customer_pool_discount, "customer_pool_default"),
        (factors.plan_pool_discount, "plan_pool_default"),
        (factors.pool_default_discount, "pool_default"),
    )
    value, source = next((value, source) for value, source in options if value is not None)
    return {
        "user_id": factors.user_id,
        "plan_id": factors.plan_id,
        "pool_id": factors.pool_id,
        "pool_name": factors.pool_name,
        "model": model,
        "discount": format(value, "f"),
        "discount_source": source,
        "customer_discount": _amount(factors.customer_discount),
        "plan_discount": _amount(factors.plan_discount),
        "customer_pool_discount": _amount(factors.customer_pool_discount),
        "plan_pool_discount": _amount(factors.plan_pool_discount),
        "pool_default_discount": format(factors.pool_default_discount, "f"),
    }


def _amount(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


async def _factors(
    connection: psycopg.AsyncConnection,
    pool_id: str,
    *,
    user_id: str | None = None,
    plan_id: str | None = None,
    model: str = "",
) -> _PoolFactors:
    if user_id is not None:
        row: Final = await _one(
            connection,
            """SELECT w.user_id,w.plan_id,r.id AS pool_id,r.name AS pool_name,r.default_discount AS pool_default_discount,
            c.discount AS customer_discount,d.discount AS plan_discount,
            a.discount AS customer_pool_discount,b.discount AS plan_pool_discount
            FROM retail_wallets w JOIN retail_resource_pools r ON r.id=%s
            LEFT JOIN retail_customer_pool_model_discounts c ON c.user_id=w.user_id AND c.pool_id=r.id AND c.model=%s
            LEFT JOIN retail_plan_pool_model_discounts d ON d.plan_id=w.plan_id AND d.pool_id=r.id AND d.model=%s
            LEFT JOIN retail_customer_pool_discounts a ON a.user_id=w.user_id AND a.pool_id=r.id
            LEFT JOIN retail_plan_pool_discounts b ON b.plan_id=w.plan_id AND b.pool_id=r.id WHERE w.user_id=%s""",
            (pool_id, model, model, user_id),
            _PoolFactors,
        )
        if row is None:
            raise WalletNotFound("The customer wallet or resource pool does not exist")
        return row
    plan: Final = await _one(
        connection,
        """SELECT NULL AS user_id,p.id AS plan_id,r.id AS pool_id,r.name AS pool_name,r.default_discount AS pool_default_discount,
        NULL AS customer_discount,d.discount AS plan_discount,NULL AS customer_pool_discount,b.discount AS plan_pool_discount
        FROM retail_pricing_plans p JOIN retail_resource_pools r ON r.id=%s
        LEFT JOIN retail_plan_pool_model_discounts d ON d.plan_id=p.id AND d.pool_id=r.id AND d.model=%s
        LEFT JOIN retail_plan_pool_discounts b ON b.plan_id=p.id AND b.pool_id=r.id WHERE p.id=%s""",
        (pool_id, model, plan_id),
        _PoolFactors,
    )
    if plan is None:
        raise PlanNotFound("The pricing plan or resource pool does not exist")
    return plan


async def get_pool_model_discount(store: WalletStore, user_id: str, model: str, pool_id: str) -> dict[str, JsonValue]:
    async with store.transaction() as connection:
        return pool_discount_view(model, await _factors(connection, pool_id, user_id=user_id, model=model))


async def pool_model_names(store: WalletStore, pool_id: str, configured: Sequence[str]) -> tuple[str, ...]:
    async with store.transaction() as connection:
        rows: Final = await _many(
            connection, "SELECT DISTINCT model FROM retail_pool_bindings WHERE pool_id=%s", (pool_id,), _ModelName
        )
        return tuple(sorted(row.model for row in rows if row.model in configured))


async def list_pool_model_discounts(
    store: WalletStore,
    pool_id: str,
    models: Sequence[str],
    *,
    user_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, JsonValue]:
    async with store.transaction() as connection:
        base: Final = await _factors(connection, pool_id, user_id=user_id, plan_id=plan_id)
        customer: Final = (
            await _many(
                connection,
                "SELECT model,discount FROM retail_customer_pool_model_discounts WHERE pool_id=%s AND user_id=%s",
                (pool_id, user_id),
                _DiscountRow,
            )
            if user_id is not None
            else ()
        )
        plan: Final = await _many(
            connection,
            "SELECT model,discount FROM retail_plan_pool_model_discounts WHERE pool_id=%s AND plan_id=%s",
            (pool_id, base.plan_id),
            _DiscountRow,
        )
        own: Final = {item.model: item.discount for item in customer}
        inherited: Final = {item.model: item.discount for item in plan}
        return {
            "user_id": user_id,
            "plan_id": base.plan_id,
            "pool_id": pool_id,
            "pool_name": base.pool_name,
            "items": [
                pool_discount_view(
                    model,
                    base.model_copy(
                        update={
                            "customer_discount": own.get(model),
                            "plan_discount": inherited.get(model),
                        }
                    ),
                )
                for model in sorted(frozenset(models) | own.keys() | inherited.keys())
            ],
        }


async def set_pool_model_discounts(
    store: WalletStore,
    pool_id: str,
    models: Sequence[str],
    discount: str | None,
    *,
    user_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, JsonValue]:
    names: Final = _discount_models(models)
    value: Final = parse_discount(discount) if discount is not None else None
    table: Final = "retail_customer_pool_model_discounts" if user_id is not None else "retail_plan_pool_model_discounts"
    column: Final = "user_id" if user_id is not None else "plan_id"
    owner: Final = user_id if user_id is not None else plan_id
    async with store.transaction() as connection:
        if user_id is not None:
            await store._lock_wallet(connection, user_id)
        else:
            await connection.execute("SELECT id FROM retail_pricing_plans WHERE id=%s FOR UPDATE", (plan_id,))
        await _factors(connection, pool_id, user_id=user_id, plan_id=plan_id)
        if value is None:
            await connection.execute(
                f"DELETE FROM {table} WHERE {column}=%s AND pool_id=%s AND model=ANY(%s::text[])",
                (owner, pool_id, list(names)),
            )
        else:
            await connection.execute(
                f"""INSERT INTO {table}({column},pool_id,model,discount)
                SELECT %s,%s,model,%s FROM unnest(%s::text[]) AS selected(model)
                ON CONFLICT({column},pool_id,model) DO UPDATE SET discount=EXCLUDED.discount""",
                (owner, pool_id, value, list(names)),
            )
    return await list_pool_model_discounts(store, pool_id, names, user_id=user_id, plan_id=plan_id)


async def pool_default_discount(
    store: WalletStore,
    pool_id: str,
    *,
    user_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, JsonValue]:
    async with store.transaction() as connection:
        base: Final = await _factors(connection, pool_id, user_id=user_id, plan_id=plan_id)
        effective: Final = pool_discount_view("", base)
        return {
            "user_id": user_id,
            "plan_id": base.plan_id,
            "pool_id": pool_id,
            "discount": _amount(base.customer_pool_discount if user_id is not None else base.plan_pool_discount),
            "plan_discount": _amount(base.plan_pool_discount),
            "pool_default_discount": format(base.pool_default_discount, "f"),
            "effective_discount": effective["discount"],
            "discount_source": effective["discount_source"],
        }


async def set_pool_default_discount(
    store: WalletStore,
    pool_id: str,
    discount: str | None,
    *,
    user_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, JsonValue]:
    value: Final = parse_discount(discount) if discount is not None else None
    table: Final = "retail_customer_pool_discounts" if user_id is not None else "retail_plan_pool_discounts"
    column: Final = "user_id" if user_id is not None else "plan_id"
    owner: Final = user_id if user_id is not None else plan_id
    async with store.transaction() as connection:
        if user_id is not None:
            await store._lock_wallet(connection, user_id)
        else:
            await connection.execute("SELECT id FROM retail_pricing_plans WHERE id=%s FOR UPDATE", (plan_id,))
        await _factors(connection, pool_id, user_id=user_id, plan_id=plan_id)
        if value is None:
            await connection.execute(f"DELETE FROM {table} WHERE {column}=%s AND pool_id=%s", (owner, pool_id))
        else:
            await connection.execute(
                f"INSERT INTO {table}({column},pool_id,discount) VALUES (%s,%s,%s) ON CONFLICT({column},pool_id) DO UPDATE SET discount=EXCLUDED.discount",
                (owner, pool_id, value),
            )
    return await pool_default_discount(store, pool_id, user_id=user_id, plan_id=plan_id)

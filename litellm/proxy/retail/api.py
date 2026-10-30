import asyncio
import hashlib
import hmac
import os
from collections.abc import Awaitable, Mapping
from decimal import Context, Decimal, InvalidOperation, localcontext
from itertools import chain
from typing import Annotated, Final, Literal, TypeAlias
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    JsonValue,
    SecretStr,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.network import TrustedProxyConfig, resolve_client_ip
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.utils import hash_password

from .expression_pricing import (
    ExpressionPolicy,
    ExpressionPricingError,
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)
from .model_price_catalog import PricingCatalogPage, PricingCatalogPrice, build_pricing_catalog
from .models import (
    IdempotencyConflict,
    InsufficientFunds,
    InvalidAmount,
    ParallelLimitExceeded,
    PlanNotFound,
    ReservationNotFound,
    WalletError,
    WalletNotFound,
    amount_to_nanos,
    cost_to_nanos,
    nanos_to_amount,
    parse_discount,
)
from .pricing import as_mapping, catalog_candidates, catalog_source_label, validated_cache_write_tiers
from .store import WalletStore, get_store

router: Final = APIRouter(prefix="/retail", tags=["Retail accounts"])


class SignupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


class CreditRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=191)
    amount: str = Field(min_length=1, max_length=40)
    reference: str = Field(min_length=1, max_length=191)
    description: str = Field(min_length=1, max_length=500)


class PlanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    discount: str | None = Field(
        default=None, min_length=1, max_length=30, validation_alias=AliasChoices("discount", "multiplier")
    )

    @field_validator("discount")
    @classmethod
    def valid_discount(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized: Final = _validated_discount(value)
        if Decimal(normalized) != 1:
            raise ValueError(
                "Use model-discounts to configure selected models. A global plan discount may only reset to 1"
            )
        return normalized


class SetDiscountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    discount: str | None = Field(min_length=1, max_length=30)

    @field_validator("discount")
    @classmethod
    def valid_discount(cls, value: str | None) -> str | None:
        if value is not None:
            raise ValueError(
                "Use model-discounts to configure selected models. The legacy global override may only be cleared"
            )
        return None


class SetModelDiscountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[Annotated[str, Field(min_length=1, max_length=191)], ...] = Field(min_length=1, max_length=500)
    discount: str | None = Field(min_length=1, max_length=30)

    @field_validator("discount")
    @classmethod
    def valid_discount(cls, value: str | None) -> str | None:
        return _validated_discount(value) if value is not None else None

    @field_validator("models")
    @classmethod
    def valid_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not model.strip() for model in value) or len(set(value)) != len(value):
            raise ValueError("Select distinct non-empty public model names")
        return value


def _validated_discount(value: str) -> str:
    try:
        return format(parse_discount(value), "f")
    except InvalidAmount as exc:
        raise ValueError(str(exc)) from exc


class SetPlanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1, max_length=80)


class ModelPriceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=191)
    input_per_million: str = Field(min_length=1, max_length=40)
    output_per_million: str = Field(min_length=1, max_length=40)
    cache_read_per_million: str = Field(min_length=1, max_length=40)
    cache_write_per_million: str = Field(min_length=1, max_length=40)
    cache_write_tiers: dict[str, str] = Field(default_factory=dict, max_length=16)
    cache_write_price_published: bool | None = Field(default=None, strict=True)
    expression_policy: ExpressionPolicy | None = None

    @field_validator("expression_policy", mode="before")
    @classmethod
    def valid_expression_policy(cls, value: object) -> ExpressionPolicy | None:
        return validate_expression_policy(value) if value is not None else None

    @field_validator("cache_write_tiers")
    @classmethod
    def valid_cache_write_tiers(cls, value: dict[str, str]) -> dict[str, str]:
        validated_cache_write_tiers({key: _per_token_price(amount) for key, amount in value.items()})
        return value


class _StoredModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    version: int
    updated_at: str
    input_cost_per_token: str
    output_cost_per_token: str
    cache_read_input_token_cost: str
    cache_creation_input_token_cost: str
    cache_write_tiers: dict[str, str] = Field(default_factory=dict)
    cache_write_price_published: bool | None = None
    expression_policy: dict[str, JsonValue] | None = None
    source: str = "legacy"
    catalog_model: str | None = None
    source_label: str = "Existing reference, review required"


class PricingPreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expression_policy: ExpressionPolicy
    facts: dict[str, StrictStr | StrictInt | dict[str, StrictStr | StrictInt]] = Field(max_length=128)
    request: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)
    started_at: AwareDatetime
    discount: str = Field(default="1", min_length=1, max_length=30)

    @field_validator("expression_policy", mode="before")
    @classmethod
    def valid_expression_policy(cls, value: object) -> ExpressionPolicy:
        return validate_expression_policy(value)

    @field_validator("discount")
    @classmethod
    def valid_discount(cls, value: str) -> str:
        return _validated_discount(value)


class ReferenceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=191)
    catalog_model: str = Field(min_length=1, max_length=300)
    source_label: str = Field(min_length=1, max_length=300)


class ReferenceImportRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ReferenceSelection, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_models(self) -> "ReferenceImportRequest":
        if len({item.model for item in self.items}) != len(self.items):
            raise ValueError("Select exactly one catalog reference per public model")
        return self


class ResolveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["release", "settle"]
    amount: str | None = Field(default=None, min_length=1, max_length=40)
    reference: str = Field(min_length=1, max_length=191)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_amount(self) -> "ResolveRequest":
        if self.action == "settle" and self.amount is None:
            raise ValueError("Settlement requires an explicit amount")
        if self.action == "release" and self.amount is not None:
            raise ValueError("Release must not include a settlement amount")
        return self


class _PublicLedgerItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    amount: str
    balance_after: str
    reference: str
    description: str
    request_id: str | None = None
    model: str | None = None
    created_at: str
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def sanitize_details(cls, value: object) -> dict[str, JsonValue]:
        return _public_ledger_details(value)


class _PublicLedgerPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[_PublicLedgerItem, ...]
    total: int
    limit: int
    offset: int


def _public_ttl_prices(value: object) -> dict[str, JsonValue]:
    try:
        return {key: amount for key, amount in validated_cache_write_tiers(value).items()}
    except ValueError:
        return {}


def _public_cost_breakdown(value: object) -> dict[str, JsonValue]:
    raw: Final = as_mapping(value)
    allowed: Final = ("input", "output", "cache_read", "cache_write_unclassified", "rounding_adjustment")
    amounts: Final = {
        key: amount for key, amount in raw.items()
        if key in allowed and isinstance(amount, str) and len(amount) <= 80 and _finite_decimal(amount)
    }
    tiers: Final = {
        key: amount for key, amount in as_mapping(raw.get("cache_write_tiers")).items()
        if _public_ttl_key(key) and isinstance(amount, str) and len(amount) <= 80 and _finite_decimal(amount)
    }
    return {
        **amounts,
        **({"cache_write_tiers": tiers} if "cache_write_tiers" in raw else {}),
    }


def _public_ttl_key(value: str) -> bool:
    return value.isascii() and value.isdecimal() and str(int(value)) == value and 1 <= int(value) <= 31_536_000


def _finite_decimal(value: str) -> bool:
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _public_expression_numbers(value: object) -> dict[str, JsonValue]:
    return {
        key: amount for key, amount in as_mapping(value).items()
        if len(key) <= 128 and key.isascii() and key.replace("_", "").replace(".", "").isalnum()
        and isinstance(amount, str) and len(amount) <= 80 and _finite_decimal(amount)
    }


def _public_expression_pricing(value: object) -> dict[str, JsonValue]:
    raw: Final = as_mapping(value)
    digest: Final = raw.get("expression_hash")
    if raw.get("version") != "expression-v1" or not isinstance(digest, str) or len(digest) != 64:
        return {}
    if any(character not in "0123456789abcdef" for character in digest):
        return {}
    expression: Final = raw.get("expression")
    if not isinstance(expression, str) or hashlib.sha256(expression.encode()).hexdigest() != digest:
        return {}
    try:
        started_at: Final = TypeAdapter(AwareDatetime).validate_python(raw.get("started_at"))
    except ValidationError:
        return {}
    strings: Final = {
        key: item for key, item in raw.items()
        if key in ("expression", "timezone") and isinstance(item, str) and 0 < len(item) <= 16_384
    }
    amounts: Final = {
        key: item for key, item in raw.items()
        if key in ("undiscounted_amount", "discount", "amount")
        and isinstance(item, str) and len(item) <= 80 and _finite_decimal(item)
    }
    tiers: Final = raw.get("tiers")
    return {
        "version": "expression-v1", "expression_hash": digest, "started_at": started_at.isoformat(),
        **strings, **amounts,
        "variables": _public_expression_numbers(raw.get("variables")),
        "usage": _public_expression_numbers(raw.get("usage")),
        "tiers": [tier for tier in tiers if isinstance(tier, str) and len(tier) <= 128]
        if isinstance(tiers, list) and len(tiers) <= 128 else [],
    }


def _public_ledger_details(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    source: Final = TypeAdapter(dict[str, JsonValue]).validate_python(value)
    usage: Final = source.get("usage")
    snapshot: Final = source.get("price_snapshot")
    allowed_usage: Final = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_write_unclassified_tokens",
        "total_tokens",
    )
    allowed_prices: Final = (
        "plan_id",
        "model",
        "version",
        "price_version",
        "multiplier",
        "discount",
        "discount_source",
        "discount_model",
        "resource_pool_id",
        "resource_pool_name",
        "routing_settings_version",
        "source",
        "source_label",
        "catalog_model",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
    )
    usage_details: Final[dict[str, JsonValue]] = (
        {
            key: item
            for key, item in usage.items()
            if key in allowed_usage and isinstance(item, int) and not isinstance(item, bool) and item >= 0
        }
        if isinstance(usage, dict)
        else {}
    )
    price_details: Final[dict[str, JsonValue]] = (
        {
            key: item
            for key, item in snapshot.items()
            if key in allowed_prices and isinstance(item, (str, int)) and not isinstance(item, bool)
        }
        if isinstance(snapshot, dict)
        else {}
    )
    ttl_counts: Final = as_mapping(usage).get("cache_write_ttl_tokens")
    if isinstance(ttl_counts, dict):
        usage_details["cache_write_ttl_tokens"] = {
            key: count
            for key, count in ttl_counts.items()
            if _public_ttl_key(key)
            and isinstance(count, int) and not isinstance(count, bool) and count >= 0
        }
    tiers: Final = _public_ttl_prices(as_mapping(snapshot).get("cache_write_tiers", {}))
    if tiers:
        price_details["cache_write_tiers"] = {key: amount for key, amount in tiers.items()}
    scalar_details: Final[dict[str, JsonValue]] = {
        key: item
        for key, item in source.items()
        if key in ("billing_basis", "gateway_call_id", "reason", "billing_error") and isinstance(item, str)
    }
    expression_pricing: Final = _public_expression_pricing(source.get("expression_pricing"))
    return {
        **scalar_details,
        **({"usage": usage_details} if usage_details else {}),
        **({"price_snapshot": price_details} if price_details else {}),
        **({"cost_breakdown": _public_cost_breakdown(source["cost_breakdown"])} if "cost_breakdown" in source else {}),
        **({"expression_pricing": expression_pricing} if expression_pricing else {}),
    }


def registration_enabled() -> bool:
    return os.environ.get("RETAIL_REGISTRATION_ENABLED", "true").lower() in ("true", "1", "yes")


async def retail_store() -> WalletStore:
    try:
        store: Final = get_store()
        await store.initialize()
        return store
    except (psycopg.Error, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Retail storage is temporarily unavailable") from exc


async def retail_user(auth: UserAPIKeyAuth = Depends(user_api_key_auth)) -> UserAPIKeyAuth:
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="A user account is required")
    return auth


async def retail_admin(auth: UserAPIKeyAuth = Depends(user_api_key_auth)) -> UserAPIKeyAuth:
    if auth.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(status_code=403, detail="Proxy administrator permission is required")
    return auth


Store: TypeAlias = Annotated[WalletStore, Depends(retail_store)]
User: TypeAlias = Annotated[UserAPIKeyAuth, Depends(retail_user)]
Admin: TypeAlias = Annotated[UserAPIKeyAuth, Depends(retail_admin)]
OperationKey: TypeAlias = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$"),
]


async def _execute(operation: Awaitable[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    try:
        return await operation
    except WalletError as exc:
        match exc:
            case WalletNotFound() | ReservationNotFound() | PlanNotFound():
                raise HTTPException(status_code=404, detail="The requested retail record does not exist") from exc
            case IdempotencyConflict():
                raise HTTPException(status_code=409, detail="The operation conflicts with an existing record") from exc
            case InsufficientFunds():
                raise HTTPException(status_code=402, detail="Insufficient wallet balance") from exc
            case ParallelLimitExceeded():
                raise HTTPException(status_code=429, detail="Too many requests are already reserved") from exc
            case _:
                raise HTTPException(status_code=400, detail="Invalid retail operation") from exc
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Retail storage is temporarily unavailable") from exc


def _source_hash(request: Request) -> str:
    client_ip, _ = resolve_client_ip(request, TrustedProxyConfig())
    secret: Final = os.environ.get("RETAIL_RATE_LIMIT_SECRET") or os.environ.get("LITELLM_MASTER_KEY", "retail-signup")
    return hmac.new(secret.encode(), (client_ip or "unknown").encode(), hashlib.sha256).hexdigest()


async def _consume_signup_limit(store: WalletStore, source_hash: str) -> bool:
    async with store.transaction() as connection:
        cursor: Final = await connection.execute(
            """INSERT INTO retail_signup_limits(source_hash,attempts) VALUES (%s,1)
               ON CONFLICT(source_hash) DO UPDATE SET
                   attempts=CASE WHEN retail_signup_limits.window_started_at < now()-interval '1 hour'
                       THEN 1 ELSE LEAST(retail_signup_limits.attempts+1,6) END,
                   window_started_at=CASE WHEN retail_signup_limits.window_started_at < now()-interval '1 hour'
                       THEN now() ELSE retail_signup_limits.window_started_at END
               RETURNING attempts""",
            (source_hash,),
        )
        row: Final = await cursor.fetchone()
        return row is not None and isinstance(row["attempts"], int) and row["attempts"] <= 5


async def _register_user(
    store: WalletStore, user_id: str, email: str, password_hash: str, display_name: str | None
) -> bool:
    async with store.transaction() as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("retail:email:" + email,))
        existing: Final = await connection.execute(
            'SELECT user_id FROM "LiteLLM_UserTable" WHERE lower(user_email)=%s LIMIT 1', (email,)
        )
        if await existing.fetchone() is not None:
            return False
        await connection.execute(
            """INSERT INTO "LiteLLM_UserTable"
               (user_id,user_email,password,user_alias,user_role,models,teams,spend,metadata,
                password_reset_required,created_at,updated_at)
               VALUES (%s,%s,%s,%s,'internal_user',ARRAY[]::text[],ARRAY[]::text[],0,'{}',false,now(),now())""",
            (user_id, email, password_hash, display_name),
        )
        await connection.execute(
            "INSERT INTO retail_signup_identities(email_normalized,user_id) VALUES (%s,%s)", (email, user_id)
        )
        await connection.execute("INSERT INTO retail_wallets(user_id) VALUES (%s)", (user_id,))
        return True


def _per_token_price(value: str) -> str:
    try:
        amount: Final = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
            raise ValueError("Price must be finite, non-negative, and no greater than 1000000000000 USD per million")
        if amount == 0:
            return "0"
        if amount < Decimal("0.000000000001"):
            raise ValueError("Price supports at most twelve decimal places per million tokens")
        per_token: Final = amount / Decimal(1000000)
        if per_token != per_token.quantize(Decimal("0.000000000000000001")):
            raise ValueError("Price supports at most twelve decimal places per million tokens")
        return format(per_token, "f")
    except InvalidOperation as exc:
        raise ValueError("Price must be a decimal USD value per million tokens") from exc


def _model_price_view(card: _StoredModelPrice) -> dict[str, JsonValue]:
    return {
        "model": card.model,
        "version": card.version,
        "updated_at": card.updated_at,
        "source": card.source,
        "catalog_model": card.catalog_model,
        "source_label": card.source_label,
        "input_per_million": format(Decimal(card.input_cost_per_token) * 1000000, "f"),
        "output_per_million": format(Decimal(card.output_cost_per_token) * 1000000, "f"),
        "cache_read_per_million": format(Decimal(card.cache_read_input_token_cost) * 1000000, "f"),
        "cache_write_per_million": format(Decimal(card.cache_creation_input_token_cost) * 1000000, "f"),
        "cache_write_tiers": {
            key: format(Decimal(value) * 1_000_000, "f") for key, value in card.cache_write_tiers.items()
        },
        "cache_write_price_published": card.cache_write_price_published,
        "expression_policy": card.expression_policy,
    }


@router.get("/config")
async def config() -> dict[str, JsonValue]:
    return {
        "registration_enabled": registration_enabled(),
        "currency": "USD",
        "login_url": "/ui/login/",
        "key_rotation_enabled": True,
    }


@router.post("/signup", status_code=201)
async def signup(data: SignupRequest, request: Request, store: Store) -> dict[str, JsonValue]:
    if not registration_enabled():
        raise HTTPException(status_code=403, detail="Registration is currently disabled")
    try:
        if not await _consume_signup_limit(store, _source_hash(request)):
            raise HTTPException(status_code=429, detail="Too many registration attempts. Try again later")
        password_hash: Final = await asyncio.to_thread(hash_password, data.password.get_secret_value())
        user_id: Final = str(uuid4())
        email: Final = str(data.email).strip().lower()
        created: Final = await _register_user(store, user_id, email, password_hash, data.display_name)
        if not created:
            raise HTTPException(status_code=409, detail="Registration could not be completed with those details")
        return {"user_id": user_id, "email": email, "login_url": "/ui/login/"}
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Registration is temporarily unavailable") from exc


@router.get("/wallet")
async def wallet(user: User, store: Store) -> dict[str, JsonValue]:
    return await _execute(store.get_wallet(user.user_id or "", create=True))


@router.get("/ledger")
async def ledger(
    user: User,
    store: Store,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    kind: Literal["credit", "charge", "release"] | None = None,
) -> dict[str, JsonValue]:
    page: Final = await _execute(store.ledger(user.user_id or "", limit=limit, offset=offset, kind=kind))
    return _PublicLedgerPage.model_validate(page).model_dump(mode="json")


@router.get("/plans")
async def plans(user: User, store: Store) -> dict[str, JsonValue]:
    if user.user_role == LitellmUserRoles.PROXY_ADMIN:
        return await _execute(store.list_plans())
    own_wallet: Final = await _execute(store.get_wallet(user.user_id or "", create=True))
    return {"items": [own_wallet["pricing_plan"]]}


def _configured_model_names() -> tuple[str, ...]:
    from litellm.proxy.proxy_server import llm_router

    return tuple(sorted(llm_router.model_names)) if llm_router is not None else ()


@router.get("/model-discounts")
async def own_model_discounts(
    user: User,
    store: Store,
    pool_id: str | None = Query(default=None, min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    await _execute(store.get_wallet(user.user_id or "", create=True))
    if pool_id is not None:
        from .pool_store import PoolStore, router_deployments

        catalog: Final = await PoolStore(store).account_catalog(user.user_id or "", router_deployments())
        items: Final = catalog.get("items")
        if not isinstance(items, list) or not any(as_mapping(item).get("id") == pool_id for item in items):
            raise HTTPException(403, "This resource pool is not available to your account")
    return await _execute(
        store.list_model_discounts(user.user_id or "", await _discount_scope_models(store, pool_id), pool_id)
    )


@router.get("/admin/discount-models")
async def admin_discount_models(admin: Admin) -> dict[str, JsonValue]:
    return {"models": list(_configured_model_names())}


@router.get("/admin/wallets/{user_id}/model-discounts")
async def admin_customer_model_discounts(
    user_id: str,
    admin: Admin,
    store: Store,
    pool_id: str | None = Query(default=None, min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(store.list_model_discounts(user_id, await _discount_scope_models(store, pool_id), pool_id))


@router.get("/admin/plans/{plan_id}/model-discounts")
async def admin_plan_model_discounts(
    plan_id: str,
    admin: Admin,
    store: Store,
    pool_id: str | None = Query(default=None, min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    return await _execute(
        store.list_plan_model_discounts(plan_id, await _discount_scope_models(store, pool_id), pool_id)
    )


async def _discount_scope_models(store: WalletStore, pool_id: str | None) -> tuple[str, ...]:
    if pool_id is None:
        return _configured_model_names()
    from .pool_pricing import pool_model_names

    return await pool_model_names(store, pool_id, _configured_model_names())


def _validate_discount_selection(
    data: SetModelDiscountRequest,
    configured: tuple[str, ...],
    current: Mapping[str, JsonValue],
) -> None:
    rows: Final = current.get("items")
    stored: Final = (
        frozenset(str(as_mapping(row).get("model")) for row in rows) if isinstance(rows, list) else frozenset()
    )
    allowed: Final = frozenset(configured) | (stored if data.discount is None else frozenset())
    if any(model not in allowed for model in data.models):
        raise HTTPException(400, "Select configured public models. Inactive stored rules may only be removed")


@router.put("/admin/wallets/{user_id}/model-discounts")
async def admin_set_customer_model_discounts(
    user_id: str,
    data: SetModelDiscountRequest,
    admin: Admin,
    store: Store,
    pool_id: str | None = Query(default=None, min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    names: Final = await _discount_scope_models(store, pool_id)
    current: Final = await _execute(store.list_model_discounts(user_id, pool_id=pool_id))
    _validate_discount_selection(data, names, current)
    await _execute(store.set_model_discounts(user_id, data.models, data.discount, pool_id))
    return await _execute(store.list_model_discounts(user_id, names, pool_id))


@router.put("/admin/plans/{plan_id}/model-discounts")
async def admin_set_plan_model_discounts(
    plan_id: str,
    data: SetModelDiscountRequest,
    admin: Admin,
    store: Store,
    pool_id: str | None = Query(default=None, min_length=1, max_length=80),
) -> dict[str, JsonValue]:
    names: Final = await _discount_scope_models(store, pool_id)
    current: Final = await _execute(store.list_plan_model_discounts(plan_id, pool_id=pool_id))
    _validate_discount_selection(data, names, current)
    await _execute(store.set_plan_model_discounts(plan_id, data.models, data.discount, pool_id))
    return await _execute(store.list_plan_model_discounts(plan_id, names, pool_id))


@router.get("/admin/wallets")
async def admin_wallets(
    admin: Admin,
    store: Store,
    query: str = Query(default="", max_length=191),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
) -> dict[str, JsonValue]:
    return await _execute(store.list_wallets(query=query, limit=limit, offset=offset))


@router.post("/admin/credit")
async def admin_credit(
    data: CreditRequest, idempotency_key: OperationKey, admin: Admin, store: Store
) -> dict[str, JsonValue]:
    try:
        amount_to_nanos(data.amount, allow_zero=False)
    except InvalidAmount as exc:
        raise HTTPException(status_code=400, detail="Credit requires a positive decimal USD amount") from exc
    return await _execute(
        store.credit(
            user_id=data.user_id,
            amount=data.amount,
            reference=data.reference,
            description=data.description,
            actor_id=admin.user_id or "proxy_admin",
            idempotency_key=idempotency_key,
        )
    )


@router.post("/admin/plans")
async def admin_plan(data: PlanRequest, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(store.save_plan(data.id, data.name, data.discount))


def _configured_references() -> tuple[tuple[str, tuple[dict[str, JsonValue], ...]], ...]:
    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        return ()
    return tuple(
        (model, catalog_candidates(model, tuple(llm_router.get_model_list(model) or ())))
        for model in sorted(llm_router.model_names)
    )


def _reference_price_view(card: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "model": card["model"],
        "catalog_model": card["catalog_model"],
        "source_label": card["source_label"],
        "input_per_million": format(Decimal(str(card["input_cost_per_token"])) * 1000000, "f"),
        "output_per_million": format(Decimal(str(card["output_cost_per_token"])) * 1000000, "f"),
        "cache_read_per_million": format(Decimal(str(card["cache_read_input_token_cost"])) * 1000000, "f"),
        "cache_write_per_million": format(Decimal(str(card["cache_creation_input_token_cost"])) * 1000000, "f"),
        "cache_write_tiers": {
            key: format(Decimal(value) * 1_000_000, "f")
            for key, value in validated_cache_write_tiers(card.get("cache_write_tiers", {})).items()
        },
        "cache_write_price_published": card.get("cache_write_price_published"),
        "expression_policy": card.get("expression_policy"),
    }


@router.get("/admin/reference-prices")
async def admin_reference_prices(admin: Admin) -> dict[str, JsonValue]:
    groups: Final = _configured_references()
    candidates: Final = chain.from_iterable(cards for _, cards in groups)
    return {
        "items": [_reference_price_view(card) for card in candidates],
        "unavailable": [
            {
                "model": model,
                "reason": "No supported flat token reference in the loaded catalog. Unknown, tiered and non-token prices require a reviewed manual reference",
            }
            for model, cards in groups
            if not cards
        ],
    }


@router.post("/admin/reference-prices/import")
async def admin_import_reference_prices(
    data: ReferenceImportRequest,
    admin: Admin,
    store: Store,
) -> dict[str, JsonValue]:
    candidates: Final = chain.from_iterable(cards for _, cards in _configured_references())
    index: Final = {(str(card["model"]), str(card["catalog_model"])): card for card in candidates}
    selected: Final = _select_references(data, index)
    saved: Final = await _execute(store.save_reference_prices(selected))
    cards: Final = TypeAdapter(tuple[_StoredModelPrice, ...]).validate_python(saved["items"])
    return {"items": [_model_price_view(card) for card in cards]}


def _select_references(
    data: ReferenceImportRequest,
    index: Mapping[tuple[str, str], dict[str, JsonValue]],
) -> tuple[dict[str, JsonValue], ...]:
    try:
        selected: Final = tuple(index[(item.model, item.catalog_model)] for item in data.items)
    except KeyError as exc:
        raise HTTPException(400, "Select a supported catalog reference for a configured public model") from exc
    if any(item.source_label != card["source_label"] for item, card in zip(data.items, selected, strict=True)):
        raise HTTPException(409, "The reference catalog changed. Refresh and review prices before importing")
    return selected


@router.get("/admin/model-prices")
async def admin_model_prices(admin: Admin, store: Store) -> dict[str, JsonValue]:
    page: Final = await _execute(store.list_model_prices())
    cards: Final = TypeAdapter(tuple[_StoredModelPrice, ...]).validate_python(page["items"])
    return {"items": [_model_price_view(card) for card in cards]}


@router.get("/admin/pricing-catalog", response_model=PricingCatalogPage)
async def admin_pricing_catalog(admin: Admin, store: Store) -> PricingCatalogPage:
    from litellm.proxy.proxy_server import llm_router

    page: Final = await _execute(store.list_model_prices())
    cards: Final = TypeAdapter(tuple[_StoredModelPrice, ...]).validate_python(page["items"])
    configured: Final = {
        model: tuple(llm_router.get_model_list(model) or ()) for model in sorted(llm_router.model_names)
    } if llm_router is not None else {}
    return build_pricing_catalog(
        configured,
        tuple(PricingCatalogPrice.model_validate(_model_price_view(card)) for card in cards),
        GetModelCostMap.loaded_model_cost_map(),
        catalog_source_label(),
    )


@router.post("/admin/model-prices")
async def admin_save_model_price(data: ModelPriceRequest, admin: Admin, store: Store) -> dict[str, JsonValue]:
    try:
        rates: Final = {
            "input_cost_per_token": _per_token_price(data.input_per_million),
            "output_cost_per_token": _per_token_price(data.output_per_million),
            "cache_read_input_token_cost": _per_token_price(data.cache_read_per_million),
            "cache_creation_input_token_cost": _per_token_price(data.cache_write_per_million),
            "cache_write_tiers": {
                key: _per_token_price(value) for key, value in data.cache_write_tiers.items()
            },
            "cache_write_price_published": data.cache_write_price_published,
            "expression_policy": data.expression_policy.model_dump(mode="json") if data.expression_policy else None,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved: Final = await _execute(store.save_model_price(data.model, rates))
    return _model_price_view(_StoredModelPrice.model_validate(saved))


@router.post("/admin/pricing-preview")
async def admin_pricing_preview(data: PricingPreviewRequest, admin: Admin) -> dict[str, JsonValue]:
    try:
        context: Final = freeze_expression_context(data.expression_policy, data.request, data.started_at)
        result: Final = evaluate_expression(data.expression_policy, data.facts, context)
    except ExpressionPricingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    undiscounted: Final = str(result["amount"])
    with localcontext(Context(prec=80)):
        discounted: Final = Decimal(undiscounted) * Decimal(data.discount)
    amount: Final = nanos_to_amount(cost_to_nanos(discounted))
    return {
        "amount": amount,
        "undiscounted_amount": undiscounted,
        "discount": data.discount,
        "tiers": result["tiers"],
        "variables": {
            **as_mapping(result["variables"]),
            **{f"cache_write_{key}": value for key, value in as_mapping(result.get("cache_write_ttl_tokens")).items()},
        },
        "usage": result.get("usage", {}),
        "started_at": data.started_at.isoformat(),
        "timezone": data.expression_policy.timezone,
        "rounding_adjustment": format(Decimal(amount) - discounted, "f"),
    }


@router.put("/admin/wallets/{user_id}/plan")
async def admin_set_plan(user_id: str, data: SetPlanRequest, admin: Admin, store: Store) -> dict[str, JsonValue]:
    return await _execute(store.set_plan(user_id, data.plan_id))


@router.put("/admin/wallets/{user_id}/discount")
async def admin_set_discount(
    user_id: str, data: SetDiscountRequest, admin: Admin, store: Store
) -> dict[str, JsonValue]:
    return await _execute(store.set_discount(user_id, data.discount))


@router.get("/admin/reservations")
async def admin_reservations(
    admin: Admin,
    store: Store,
    state: Literal["reserved", "settled", "released"] = "reserved",
    user_id: str | None = Query(default=None, max_length=191),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
) -> dict[str, JsonValue]:
    return await _execute(store.list_reservations(user_id=user_id, state=state, limit=limit, offset=offset))


@router.post("/admin/reservations/{request_id}/resolve")
async def admin_resolve(
    request_id: str,
    data: ResolveRequest,
    idempotency_key: OperationKey,
    admin: Admin,
    store: Store,
) -> dict[str, JsonValue]:
    details: Final[dict[str, JsonValue]] = {
        "actor_id": admin.user_id or "proxy_admin",
        "reference": data.reference,
        "reason": data.reason,
        "idempotency_key": idempotency_key,
    }
    if data.action == "release":
        return await _execute(store.release(request_id, data.reason, details=details))
    return await _execute(store.settle(request_id, data.amount or "0", details=details))

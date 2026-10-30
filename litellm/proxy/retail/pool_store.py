from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from itertools import chain
from typing import Final, Literal

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

from .models import InvalidAmount, parse_discount
from .pricing import as_mapping
from .store import WalletStore, _many, _one, get_store


class PoolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    source_kind: Literal["anthropic", "openai", "bedrock", "vertex", "anthropic_subscribe", "custom"] = "custom"
    description: str = Field(default="", max_length=1000)
    enabled: bool = False
    visibility: Literal["public", "restricted"] = "restricted"
    priority: int = Field(default=100, ge=0, le=1000000)
    default_discount: str = "1"

    @field_validator("default_discount", mode="before")
    @classmethod
    def validate_discount(cls, value: object) -> str:
        try:
            return format(parse_discount(str(value)), "f")
        except InvalidAmount as exc:
            raise ValueError("Pool discount must be greater than zero and at most 1") from exc


class PoolBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: str = Field(min_length=1, max_length=191)
    model: str = Field(min_length=1, max_length=191)
    weight: int = Field(default=1, ge=1, le=1000000)
    priority: int = Field(default=0, ge=0, le=1000000)


class PoolGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_id: str = Field(min_length=1, max_length=80)
    models: Literal["*"] | tuple[str, ...] = "*"

    @field_validator("models")
    @classmethod
    def valid_models(cls, value: Literal["*"] | tuple[str, ...]) -> Literal["*"] | tuple[str, ...]:
        if value != "*" and (
            not value
            or len(value) > 500
            or len(set(value)) != len(value)
            or any(not model.strip() or len(model) > 191 for model in value)
        ):
            raise ValueError("Each pool grant needs distinct public model names or *")
        return value


class PoolAccess(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["inherit", "custom"] = "inherit"
    grants: tuple[PoolGrant, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def valid_grants(self) -> "PoolAccess":
        if self.mode == "inherit" and self.grants:
            raise ValueError("Inherited access does not include custom grants")
        _unique_grants(self.grants)
        return self


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["auto", "fixed", "custom"] = "auto"
    pool_id: str | None = Field(default=None, min_length=1, max_length=80)
    grants: tuple[PoolGrant, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def valid_policy(self) -> "ResourcePolicy":
        if self.mode == "fixed" and (not self.pool_id or self.grants):
            raise ValueError("Fixed routing requires one pool_id and no custom grants")
        if self.mode == "auto" and (self.pool_id is not None or self.grants):
            raise ValueError("Automatic routing inherits account pool grants")
        if self.mode == "custom" and (self.pool_id is not None or not self.grants):
            raise ValueError("Custom routing requires paired pool/model grants")
        _unique_grants(self.grants)
        return self


def _unique_grants(grants: Sequence[PoolGrant]) -> None:
    if len({grant.pool_id for grant in grants}) != len(grants):
        raise ValueError("Each pool may appear only once in grants")


class DeploymentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    deployment_id: str
    model: str
    provider: str
    credential_name: str | None = None


class PoolRouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    pool_id: str
    pool_name: str
    source_kind: Literal["anthropic", "openai", "bedrock", "vertex", "anthropic_subscribe", "custom"] = "custom"
    allowed_deployment_ids: tuple[str, ...]
    bindings: tuple[PoolBinding, ...]
    settings_version: int
    pool_version: int


class _PoolRow(PoolDefinition):
    version: int
    updated_at: datetime


class _StoredBinding(PoolBinding):
    pool_id: str


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    routing_enabled: bool
    version: int


class _StoredAccess(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["inherit", "custom"]
    grants: tuple[PoolGrant, ...]


class _UserId(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str


def deployment_catalog(deployments: Sequence[Mapping[str, object]]) -> tuple[DeploymentRecord, ...]:
    records: Final = tuple(_deployment_record(item) for item in deployments)
    valid: Final = tuple(item for item in records if item is not None)
    return tuple({(item.deployment_id, item.model): item for item in valid}.values())


def _deployment_record(deployment: Mapping[str, object]) -> DeploymentRecord | None:
    info: Final = as_mapping(deployment.get("model_info"))
    params: Final = as_mapping(deployment.get("litellm_params"))
    identifier: Final = info.get("id")
    model: Final = deployment.get("model_name")
    if not isinstance(identifier, str) or not identifier or not isinstance(model, str) or not model:
        return None
    upstream: Final = str(params.get("model", ""))
    provider: Final = str(
        params.get("custom_llm_provider") or (upstream.partition("/")[0] if "/" in upstream else "openai")
    )
    credential: Final = params.get("litellm_credential_name")
    return DeploymentRecord(
        deployment_id=identifier,
        model=model,
        provider=provider,
        credential_name=credential if isinstance(credential, str) else None,
    )


def validate_bindings(bindings: Sequence[PoolBinding], deployments: Sequence[DeploymentRecord]) -> None:
    actual: Final = frozenset((item.deployment_id, item.model) for item in deployments)
    selected: Final = tuple((item.deployment_id, item.model) for item in bindings)
    if len(set(selected)) != len(selected):
        raise HTTPException(400, "Each deployment/public-model binding must be unique")
    if any(item not in actual for item in selected):
        raise HTTPException(400, "Bindings must match an existing deployment and its exact public model")


def _binding_model(binding: _StoredBinding) -> PoolBinding:
    return PoolBinding.model_validate(binding.model_dump(exclude={"pool_id"}))


def _pool_view(pool: _PoolRow, bindings: Sequence[_StoredBinding]) -> dict[str, JsonValue]:
    own: Final = tuple(binding for binding in bindings if binding.pool_id == pool.id)
    return {
        **pool.model_dump(mode="json"),
        "bindings": [_binding_model(binding).model_dump(mode="json") for binding in own],
        "models": sorted({binding.model for binding in own}),
    }


def effective_account_grants(
    access: PoolAccess,
    pools: Sequence[PoolDefinition],
    bindings: Sequence[_StoredBinding],
) -> tuple[PoolGrant, ...]:
    enabled: Final = {pool.id: pool for pool in pools if pool.enabled}
    requested: Final = (
        tuple(PoolGrant(pool_id=pool.id) for pool in pools if pool.enabled and pool.visibility == "public")
        if access.mode == "inherit"
        else access.grants
    )
    return tuple(grant for item in requested if (grant := _materialize_grant(item, enabled, bindings)) is not None)


def _materialize_grant(
    grant: PoolGrant,
    pools: Mapping[str, PoolDefinition],
    bindings: Sequence[_StoredBinding],
) -> PoolGrant | None:
    if grant.pool_id not in pools:
        return None
    models: Final = frozenset(binding.model for binding in bindings if binding.pool_id == grant.pool_id)
    selected: Final = models if grant.models == "*" else models.intersection(grant.models)
    return PoolGrant(pool_id=grant.pool_id, models=tuple(sorted(selected))) if selected else None


def effective_key_grants(
    policy: ResourcePolicy,
    account: Sequence[PoolGrant],
    *,
    strict: bool,
) -> tuple[PoolGrant, ...]:
    if policy.mode == "auto":
        return tuple(account)
    requested: Final = (PoolGrant(pool_id=policy.pool_id or ""),) if policy.mode == "fixed" else policy.grants
    allowed: Final = {grant.pool_id: grant for grant in account}
    return tuple(
        grant for item in requested if (grant := _intersect_grant(item, allowed.get(item.pool_id), strict)) is not None
    )


def _intersect_grant(requested: PoolGrant, allowed: PoolGrant | None, strict: bool) -> PoolGrant | None:
    if allowed is None:
        if strict:
            raise HTTPException(403, "The selected resource pool is not available to this account")
        return None
    if requested.models == "*":
        return allowed
    models: Final = (
        tuple(requested.models)
        if allowed.models == "*"
        else tuple(model for model in requested.models if model in allowed.models)
    )
    if strict and len(models) != len(requested.models):
        raise HTTPException(403, "The selected pool/model pair is not allowed for this account")
    return PoolGrant(pool_id=requested.pool_id, models=models) if models else None


def policy_payload(policy: ResourcePolicy, grants: Sequence[PoolGrant] = ()) -> dict[str, JsonValue]:
    if policy.mode == "fixed":
        return {"mode": "fixed", "pool_id": policy.pool_id}
    if policy.mode == "custom":
        return {"mode": "custom", "grants": [grant.model_dump(mode="json") for grant in grants]}
    return {"mode": "auto"}


def choose_route(
    settings: _Settings,
    pools: Sequence[_PoolRow],
    bindings: Sequence[_StoredBinding],
    access: PoolAccess,
    model: str,
    policy: object | None,
    deployments: Sequence[Mapping[str, object]],
) -> PoolRouteDecision | None:
    if not settings.routing_enabled and policy is None:
        return None
    routing: Final = _runtime_policy(policy)
    if routing.mode == "fixed":
        fixed: Final = next((pool for pool in pools if pool.id == routing.pool_id and pool.enabled), None)
        if fixed is not None and not any(binding.pool_id == fixed.id for binding in bindings):
            authorized: Final = (
                fixed.visibility == "public"
                if access.mode == "inherit"
                else any(
                    grant.pool_id == fixed.id and (grant.models == "*" or model in grant.models)
                    for grant in access.grants
                )
            )
            if authorized:
                raise HTTPException(503, "The selected resource pool has no upstream deployments yet")
    account: Final = effective_account_grants(access, pools, bindings)
    grants: Final = effective_key_grants(routing, account, strict=False)
    eligible: Final = frozenset(grant.pool_id for grant in grants if grant.models == "*" or model in grant.models)
    if not eligible:
        raise HTTPException(403, "This API key and account do not allow the requested pool/model pair")
    live: Final = _live_bindings(bindings, deployments)
    candidates: Final = tuple(
        pool
        for pool in pools
        if pool.id in eligible and any(item.pool_id == pool.id and item.model == model for item in live)
    )
    if not candidates:
        raise HTTPException(503, "No deployment is available in an allowed resource pool")
    selected: Final = min(candidates, key=lambda pool: (pool.priority, pool.id))
    chosen: Final = tuple(_binding_model(item) for item in live if item.pool_id == selected.id and item.model == model)
    return PoolRouteDecision(
        pool_id=selected.id,
        pool_name=selected.name,
        source_kind=selected.source_kind,
        allowed_deployment_ids=tuple(dict.fromkeys(item.deployment_id for item in chosen)),
        bindings=chosen,
        settings_version=settings.version,
        pool_version=selected.version,
    )


def allowed_model_names(
    access: PoolAccess,
    pools: Sequence[PoolDefinition],
    bindings: Sequence[_StoredBinding],
    policy: object | None,
    deployments: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    live: Final = _live_bindings(bindings, deployments)
    account: Final = effective_account_grants(access, pools, live)
    grants: Final = effective_key_grants(_runtime_policy(policy), account, strict=False)
    return frozenset(chain.from_iterable(grant.models for grant in grants if grant.models != "*"))


def allowed_deployment_ids(
    access: PoolAccess,
    pools: Sequence[PoolDefinition],
    bindings: Sequence[_StoredBinding],
    policy: object | None,
    deployments: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    live: Final = _live_bindings(bindings, deployments)
    account: Final = effective_account_grants(access, pools, live)
    grants: Final = {grant.pool_id: grant for grant in effective_key_grants(_runtime_policy(policy), account, strict=False)}
    return frozenset(
        binding.deployment_id
        for binding in live
        if binding.pool_id in grants and binding.model in grants[binding.pool_id].models
    )


def router_deployments() -> tuple[Mapping[str, object], ...]:
    from litellm.proxy.proxy_server import llm_router

    return tuple(llm_router.model_list) if llm_router is not None else ()


class PoolStore:
    def __init__(self, wallet_store: WalletStore | None = None) -> None:
        self.wallet_store = wallet_store if wallet_store is not None else get_store()

    @asynccontextmanager
    async def _read_transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.wallet_store.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            yield connection

    async def _settings(self, connection: psycopg.AsyncConnection, lock: bool = False) -> _Settings:
        row: Final = await _one(
            connection,
            "SELECT routing_enabled,version FROM retail_pool_settings WHERE singleton=true"
            + (" FOR UPDATE" if lock else ""),
            (),
            _Settings,
        )
        if row is None:
            raise HTTPException(503, "Resource pool settings are unavailable")
        return row

    async def _state(
        self, connection: psycopg.AsyncConnection
    ) -> tuple[tuple[_PoolRow, ...], tuple[_StoredBinding, ...]]:
        pools: Final = await _many(connection, "SELECT * FROM retail_resource_pools ORDER BY priority,id", (), _PoolRow)
        bindings: Final = await _many(
            connection,
            """SELECT b.* FROM retail_pool_bindings b
            LEFT JOIN retail_channel_deployments m ON m.deployment_id=b.deployment_id
            LEFT JOIN retail_channels c ON c.id=m.channel_id
            WHERE c.id IS NULL OR c.status='active' OR
            (c.pending_deployment_ids<>'[]'::jsonb AND NOT c.pending_deployment_ids ? b.deployment_id)
            ORDER BY b.pool_id,b.priority,b.deployment_id,b.model""",
            (),
            _StoredBinding,
        )
        return pools, bindings

    async def _access(self, connection: psycopg.AsyncConnection, user_id: str) -> PoolAccess:
        row: Final = await _one(
            connection,
            "SELECT mode,grants FROM retail_account_pool_access WHERE user_id=%s",
            (user_id,),
            _StoredAccess,
        )
        return PoolAccess(mode=row.mode, grants=row.grants) if row is not None else PoolAccess()

    async def list_pools(self) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            pools, bindings = await self._state(connection)
            return {"items": [_pool_view(pool, bindings) for pool in pools]}

    async def save_pool(
        self,
        definition: PoolDefinition,
        deployments: Sequence[Mapping[str, object]],
        bindings: Sequence[PoolBinding] | None = None,
        *,
        create_only: bool = False,
        update_only: bool = False,
    ) -> dict[str, JsonValue]:
        catalog: Final = deployment_catalog(deployments)
        if bindings is not None:
            validate_bindings(bindings, catalog)
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() as connection:
            await self._settings(connection, lock=True)
            current: Final = await _one(
                connection,
                "SELECT * FROM retail_resource_pools WHERE id=%s FOR UPDATE",
                (definition.id,),
                _PoolRow,
            )
            if create_only and current is not None:
                raise HTTPException(409, "This pool ID already exists. Edit the existing resource pool instead")
            if update_only and current is None:
                raise HTTPException(404, "Resource pool does not exist")
            existing: Final = await _many(
                connection,
                "SELECT * FROM retail_pool_bindings WHERE pool_id=%s",
                (definition.id,),
                _StoredBinding,
            )
            selected: Final = (
                tuple(bindings) if bindings is not None else tuple(_binding_model(item) for item in existing)
            )
            if definition.enabled:
                validate_bindings(selected, catalog)
            from .channel_store import validate_channel_bindings

            await validate_channel_bindings(connection, definition.id, selected)
            row: Final = await _one(
                connection,
                """INSERT INTO retail_resource_pools
                (id,name,source_kind,description,enabled,visibility,priority,default_discount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,source_kind=EXCLUDED.source_kind,
                description=EXCLUDED.description,enabled=EXCLUDED.enabled,visibility=EXCLUDED.visibility,
                priority=EXCLUDED.priority,default_discount=EXCLUDED.default_discount,
                version=retail_resource_pools.version+1,updated_at=now() RETURNING *""",
                (
                    definition.id,
                    definition.name,
                    definition.source_kind,
                    definition.description,
                    definition.enabled,
                    definition.visibility,
                    definition.priority,
                    definition.default_discount,
                ),
                _PoolRow,
            )
            if row is None:
                raise HTTPException(503, "The resource pool could not be saved")
            if bindings is not None:
                await self._replace_bindings(connection, definition.id, selected)
            saved: Final = tuple(_StoredBinding(pool_id=definition.id, **item.model_dump()) for item in selected)
            return _pool_view(row, saved)

    async def _replace_bindings(
        self,
        connection: psycopg.AsyncConnection,
        pool_id: str,
        bindings: Sequence[PoolBinding],
    ) -> None:
        await connection.execute("DELETE FROM retail_pool_bindings WHERE pool_id=%s", (pool_id,))
        for binding in bindings:
            await connection.execute(
                "INSERT INTO retail_pool_bindings(pool_id,deployment_id,model,weight,priority) VALUES (%s,%s,%s,%s,%s)",
                (pool_id, binding.deployment_id, binding.model, binding.weight, binding.priority),
            )

    async def replace_bindings(
        self,
        pool_id: str,
        bindings: Sequence[PoolBinding],
        deployments: Sequence[Mapping[str, object]],
    ) -> dict[str, JsonValue]:
        validate_bindings(bindings, deployment_catalog(deployments))
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() as connection:
            await self._settings(connection, lock=True)
            from .channel_store import validate_channel_bindings

            await validate_channel_bindings(connection, pool_id, bindings)
            row: Final = await _one(
                connection, "SELECT * FROM retail_resource_pools WHERE id=%s FOR UPDATE", (pool_id,), _PoolRow
            )
            if row is None:
                raise HTTPException(404, "Resource pool does not exist")
            await self._replace_bindings(connection, pool_id, bindings)
            updated: Final = await _one(
                connection,
                "UPDATE retail_resource_pools SET version=version+1,updated_at=now() WHERE id=%s RETURNING *",
                (pool_id,),
                _PoolRow,
            )
            if updated is None:
                raise HTTPException(503, "The resource pool could not be saved")
            saved: Final = tuple(_StoredBinding(pool_id=pool_id, **item.model_dump()) for item in bindings)
            return _pool_view(updated, saved)

    async def disable_pool(self, pool_id: str) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() as connection:
            await self._settings(connection, lock=True)
            row: Final = await _one(
                connection,
                "UPDATE retail_resource_pools SET enabled=false,version=version+1,updated_at=now() WHERE id=%s RETURNING *",
                (pool_id,),
                _PoolRow,
            )
            if row is None:
                raise HTTPException(404, "Resource pool does not exist")
            bindings: Final = await _many(
                connection, "SELECT * FROM retail_pool_bindings WHERE pool_id=%s", (pool_id,), _StoredBinding
            )
            return _pool_view(row, bindings)

    async def settings(
        self,
        deployments: Sequence[Mapping[str, object]],
        routing_enabled: bool | None = None,
    ) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        catalog: Final = deployment_catalog(deployments)
        async with (
            self.wallet_store.transaction() if routing_enabled is not None else self._read_transaction()
        ) as connection:
            settings: Final = await self._settings(connection, lock=routing_enabled is not None)
            pools, bindings = await self._state(connection)
            actual: Final = frozenset((item.deployment_id, item.model) for item in catalog)
            enabled: Final = frozenset(pool.id for pool in pools if pool.enabled)
            active: Final = tuple(
                item for item in bindings if item.pool_id in enabled and (item.deployment_id, item.model) in actual
            )
            if routing_enabled and not active:
                raise HTTPException(400, "Enable and bind at least one resource pool before activating pool routing")
            if routing_enabled is not None:
                await connection.execute(
                    "UPDATE retail_pool_settings SET routing_enabled=%s,version=version+1 WHERE singleton=true",
                    (routing_enabled,),
                )
            return {
                "routing_enabled": settings.routing_enabled if routing_enabled is None else routing_enabled,
                "version": settings.version + (1 if routing_enabled is not None else 0),
                "unassigned_models": sorted({item.model for item in catalog} - {item.model for item in active}),
            }

    async def account_access(
        self,
        user_id: str,
        deployments: Sequence[Mapping[str, object]],
        access: PoolAccess | None = None,
    ) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() if access is not None else self._read_transaction() as connection:
            if access is not None:
                await self._settings(connection, lock=True)
                user: Final = await _one(
                    connection, 'SELECT user_id FROM "LiteLLM_UserTable" WHERE user_id=%s', (user_id,), _UserId
                )
                if user is None:
                    raise HTTPException(404, "User account does not exist")
                pools, bindings = await self._state(connection)
                _validate_account_grants(access, pools, bindings)
                await connection.execute(
                    "INSERT INTO retail_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING", (user_id,)
                )
                await connection.execute(
                    """INSERT INTO retail_account_pool_access(user_id,mode,grants) VALUES (%s,%s,%s)
                    ON CONFLICT(user_id) DO UPDATE SET mode=EXCLUDED.mode,grants=EXCLUDED.grants,updated_at=now()""",
                    (user_id, access.mode, Jsonb([grant.model_dump(mode="json") for grant in access.grants])),
                )
            saved: Final = access if access is not None else await self._access(connection, user_id)
            current_pools, current_bindings = await self._state(connection)
            live: Final = _live_bindings(current_bindings, deployments)
            grants: Final = effective_account_grants(saved, current_pools, live)
            return {
                "user_id": user_id,
                **saved.model_dump(mode="json"),
                "effective_grants": [grant.model_dump(mode="json") for grant in grants],
            }

    async def account_catalog(self, user_id: str, deployments: Sequence[Mapping[str, object]]) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            settings: Final = await self._settings(connection)
            pools, bindings = await self._state(connection)
            access: Final = await self._access(connection, user_id)
            live: Final = _live_bindings(bindings, deployments)
            grants: Final = {grant.pool_id: grant for grant in effective_account_grants(access, pools, live)}
            return {
                "routing_enabled": settings.routing_enabled,
                "policy_modes": ["auto", "fixed", "custom"],
                "items": [
                    {
                        **pool.model_dump(mode="json"),
                        "models": list(grants[pool.id].models),
                        "allowed_models": list(grants[pool.id].models),
                    }
                    for pool in pools
                    if pool.id in grants
                ],
            }

    async def validate_key_policy(self, user_id: str, policy: object) -> dict[str, JsonValue]:
        if not user_id:
            raise HTTPException(403, "Resource policies require a key owned by a user account")
        parsed: Final = _parse_policy(policy)
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            pools, bindings = await self._state(connection)
            access: Final = await self._access(connection, user_id)
            grants: Final = effective_account_grants(access, pools, _live_bindings(bindings, router_deployments()))
            selected: Final = effective_key_grants(parsed, grants, strict=True)
            if not selected:
                raise HTTPException(403, "No resource pools are available to this account")
            return policy_payload(parsed, selected)

    async def resolve_route(
        self,
        user_id: str,
        model: str,
        policy: object | None,
        deployments: Sequence[Mapping[str, object]],
    ) -> PoolRouteDecision | None:
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            settings: Final = await self._settings(connection)
            if not settings.routing_enabled and policy is None:
                return None
            if not user_id:
                raise HTTPException(403, "Resource pool routing requires a key owned by a user account")
            pools, bindings = await self._state(connection)
            access: Final = await self._access(connection, user_id)
            return choose_route(settings, pools, bindings, access, model, policy, deployments)

    async def key_model_names(
        self,
        user_id: str,
        policy: object | None,
        deployments: Sequence[Mapping[str, object]],
    ) -> frozenset[str] | None:
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            settings: Final = await self._settings(connection)
            if not settings.routing_enabled and policy is None:
                return None
            if not user_id:
                raise HTTPException(403, "Resource pool discovery requires a key owned by a user account")
            pools, bindings = await self._state(connection)
            access: Final = await self._access(connection, user_id)
            return allowed_model_names(access, pools, bindings, policy, deployments)

    async def key_deployment_ids(
        self, user_id: str, policy: object | None, deployments: Sequence[Mapping[str, object]],
    ) -> frozenset[str] | None:
        await self.wallet_store.initialize()
        async with self._read_transaction() as connection:
            settings: Final = await self._settings(connection)
            if not settings.routing_enabled and policy is None:
                return None
            if not user_id:
                raise HTTPException(403, "Resource pool discovery requires a key owned by a user account")
            pools, bindings = await self._state(connection)
            access: Final = await self._access(connection, user_id)
            return allowed_deployment_ids(access, pools, bindings, policy, deployments)


def _live_bindings(
    bindings: Sequence[_StoredBinding],
    deployments: Sequence[Mapping[str, object]],
) -> tuple[_StoredBinding, ...]:
    actual: Final = frozenset((item.deployment_id, item.model) for item in deployment_catalog(deployments))
    return tuple(item for item in bindings if (item.deployment_id, item.model) in actual)


def _validate_account_grants(
    access: PoolAccess,
    pools: Sequence[PoolDefinition],
    bindings: Sequence[_StoredBinding],
) -> None:
    existing: Final = frozenset(pool.id for pool in pools)
    for grant in access.grants:
        if grant.pool_id not in existing:
            raise HTTPException(400, "Account grants must reference an existing resource pool")
        models: Final = frozenset(item.model for item in bindings if item.pool_id == grant.pool_id)
        if grant.models != "*" and not set(grant.models).issubset(models):
            raise HTTPException(400, "Account model grants must match bindings within the same resource pool")


def _parse_policy(policy: object) -> ResourcePolicy:
    try:
        return ResourcePolicy.model_validate(policy)
    except ValidationError as exc:
        raise HTTPException(
            422, "Invalid resource policy. Use auto, one fixed pool, or paired custom pool/model grants"
        ) from exc


def _runtime_policy(policy: object | None) -> ResourcePolicy:
    if policy is None:
        return ResourcePolicy()
    try:
        return _parse_policy(policy)
    except HTTPException as exc:
        raise HTTPException(403, "This API key contains an invalid resource policy") from exc


async def validate_key_resource_policy(
    policy: object,
    user_id: str,
    store: WalletStore | None = None,
) -> dict[str, JsonValue]:
    return await PoolStore(store).validate_key_policy(user_id, policy)

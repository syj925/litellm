from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from json import dumps
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator

from .pool_store import DeploymentRecord, PoolBinding, PoolStore, _PoolRow, _StoredBinding, deployment_catalog
from .pricing import as_mapping
from .store import WalletStore, _many, _one


class ChannelDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    pool_id: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A channel name is required")
        return value.strip()


class ChannelUpdate(ChannelDetails):
    deployment_ids: tuple[str, ...] = Field(max_length=2000)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("deployment_ids")
    @classmethod
    def distinct_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or len(item) > 191 for item in value):
            raise ValueError("Choose distinct deployment IDs")
        return value


class ChannelRow(ChannelDetails):
    model_config = ConfigDict(frozen=True)

    id: str
    status: Literal["active", "provisioning", "error"]
    version: int
    updated_at: datetime
    pending_deployment_ids: tuple[str, ...] = ()


class ChannelMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    deployment_id: str
    model: str


class _OperationLock(BaseModel):
    acquired: bool


def channel_view(channel: ChannelRow, members: Sequence[ChannelMember], pool_name: str) -> dict[str, JsonValue]:
    own: Final = tuple(item for item in members if item.channel_id == channel.id)
    return {
        **channel.model_dump(mode="json"),
        "pool_name": pool_name,
        "deployment_ids": [item.deployment_id for item in own],
        "models": sorted({item.model for item in own}),
        "deployment_count": len(own),
    }


def selected_members(
    channel_id: str, identifiers: Sequence[str], deployments: Sequence[DeploymentRecord]
) -> tuple[ChannelMember, ...]:
    by_id: Final = {item.deployment_id: item for item in deployments}
    if any(identifier not in by_id for identifier in identifiers):
        raise HTTPException(400, "Every selected deployment must exist in the current gateway")
    return tuple(
        ChannelMember(channel_id=channel_id, deployment_id=identifier, model=by_id[identifier].model)
        for identifier in identifiers
    )


def _identity_value(value: object) -> JsonValue:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _identity_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_identity_value(item) for item in value]
    return None


def _usable_identity(value: JsonValue) -> bool:
    return bool(value) and not (
        isinstance(value, str) and (set(value) == {"*"} or value.lower() in ("[redacted]", "<redacted>"))
    )


def connection_details(deployment: Mapping[str, object]) -> tuple[str, str | None]:
    params: Final = as_mapping(deployment.get("litellm_params"))
    upstream: Final = str(params.get("model", ""))
    provider: Final = str(
        params.get("custom_llm_provider") or (upstream.partition("/")[0] if "/" in upstream else "openai")
    )
    base: Final = str(params.get("api_base") or "")
    credential_name: Final = _identity_value(params.get("litellm_credential_name"))
    credentials: Final = {
        key: _identity_value(params.get(key))
        for key in (
            "api_key",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_web_identity_token",
            "aws_external_id",
            "vertex_credentials",
        )
        if params.get(key) is not None
    }
    locations: Final = {
        key: _identity_value(params.get(key))
        for key in (
            "region_name",
            "aws_region_name",
            "aws_profile_name",
            "aws_role_name",
            "aws_session_name",
            "aws_session_tags",
            "aws_sts_endpoint",
            "aws_bedrock_runtime_endpoint",
            "vertex_project",
            "vertex_location",
        )
        if params.get(key) is not None
    }
    reliable: Final = (
        _usable_identity(credential_name)
        or _usable_identity(credentials.get("api_key"))
        or (provider.startswith("vertex_ai") and _usable_identity(credentials.get("vertex_credentials")))
        or (
            provider.startswith(("bedrock", "sagemaker"))
            and (
                (
                    _usable_identity(credentials.get("aws_access_key_id"))
                    and _usable_identity(credentials.get("aws_secret_access_key"))
                )
                or _usable_identity(locations.get("aws_profile_name"))
                or _usable_identity(locations.get("aws_role_name"))
            )
        )
    )
    info: Final = as_mapping(deployment.get("model_info"))
    credential: Final = {"saved": credential_name} if _usable_identity(credential_name) else credentials
    identity: Final = sha256(
        dumps(
            (provider, base, credential, locations) if reliable else ("deployment", str(info.get("id") or uuid4())),
            ensure_ascii=True,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        parsed: Final = urlsplit(base)
        hostname: Final = parsed.hostname
        host: Final = f"[{hostname}]" if hostname and ":" in hostname else hostname
        return identity, f"{host}:{parsed.port}" if host and parsed.port else host
    except ValueError:
        return identity, None


class ChannelStore:
    def __init__(self, wallet_store: WalletStore) -> None:
        self.wallet_store = wallet_store
        self.pools = PoolStore(wallet_store)

    @asynccontextmanager
    async def operation_lock(self, channel_id: str) -> AsyncIterator[None]:
        async with await psycopg.AsyncConnection.connect(
            self.wallet_store.database_url, row_factory=dict_row
        ) as connection:
            async with connection.transaction():
                lock: Final = await _one(
                    connection,
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                    ("retail:channel:" + channel_id,),
                    _OperationLock,
                )
                if lock is None or not lock.acquired:
                    raise HTTPException(
                        409, "A channel deployment operation is still running. Try again after it finishes"
                    )
                yield

    async def _rows(
        self, connection: psycopg.AsyncConnection
    ) -> tuple[tuple[ChannelRow, ...], tuple[ChannelMember, ...]]:
        channels: Final = await _many(connection, "SELECT * FROM retail_channels ORDER BY name,id", (), ChannelRow)
        members: Final = await _many(
            connection,
            "SELECT * FROM retail_channel_deployments ORDER BY channel_id,model,deployment_id",
            (),
            ChannelMember,
        )
        return channels, members

    async def list_channels(self) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self.pools._read_transaction() as connection:
            channels, members = await self._rows(connection)
            pools, _ = await self.pools._state(connection)
            names: Final = {item.id: item.name for item in pools}
            return {"enabled": True, "items": [channel_view(item, members, names[item.pool_id]) for item in channels]}

    async def catalog(self, deployments: Sequence[Mapping[str, object]]) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self.pools._read_transaction() as connection:
            channels, members = await self._rows(connection)
            pools, bindings = await self.pools._state(connection)
            by_channel: Final = {item.id: item for item in channels}
            by_deployment: Final = {item.deployment_id: by_channel[item.channel_id] for item in members}
            pool_names: Final = {item.id: item.name for item in pools}
            connections: Final = {
                str(as_mapping(item.get("model_info")).get("id")): connection_details(item) for item in deployments
            }
            return {
                "items": [
                    {
                        **item.model_dump(mode="json"),
                        "channel_id": owner.id if (owner := by_deployment.get(item.deployment_id)) else None,
                        "channel_name": owner.name if owner else None,
                        "pool_id": owner.pool_id if owner else None,
                        "pool_name": pool_names[owner.pool_id] if owner else None,
                        "channel_status": owner.status if owner else None,
                        "connection_id": connections[item.deployment_id][0],
                        "endpoint_host": connections[item.deployment_id][1],
                        "bound_pool_ids": sorted(
                            {binding.pool_id for binding in bindings if binding.deployment_id == item.deployment_id}
                        ),
                    }
                    for item in deployment_catalog(deployments)
                ]
            }

    async def save_existing(
        self, details: ChannelUpdate, deployments: Sequence[Mapping[str, object]], channel_id: str | None = None
    ) -> dict[str, JsonValue]:
        identifier: Final = channel_id or str(uuid4())
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() as connection:
            return await self.save_existing_in_transaction(
                connection, details, deployments, identifier, channel_id is not None
            )

    async def save_existing_in_transaction(
        self,
        connection: psycopg.AsyncConnection,
        details: ChannelUpdate,
        deployments: Sequence[Mapping[str, object]],
        channel_id: str,
        require_existing: bool = True,
    ) -> dict[str, JsonValue]:
        members: Final = selected_members(channel_id, details.deployment_ids, deployment_catalog(deployments))
        await self.pools._settings(connection, lock=True)
        current: Final = await self._channel(connection, channel_id)
        if require_existing and current is None:
            raise HTTPException(404, "Channel does not exist")
        if current is not None and current.status != "active":
            raise HTTPException(409, "Recover the unfinished channel deployment operation before editing")
        if current is not None and details.expected_version != current.version:
            raise HTTPException(409, "The channel changed. Reload it before saving your edits")
        pool: Final = await self._pool(connection, details.pool_id)
        await self._validate_name(connection, details.name, channel_id)
        await self._validate_members(connection, channel_id, members)
        row: Final = await self._save_row(connection, channel_id, details, "active")
        await self._replace_members(connection, row, members, current)
        return channel_view(row, members, pool.name)

    async def reserve_new(
        self, details: ChannelDetails, members: Sequence[ChannelMember], channel_id: str
    ) -> dict[str, JsonValue]:
        await self.wallet_store.initialize()
        async with self.wallet_store.transaction() as connection:
            await self.pools._settings(connection, lock=True)
            current: Final = await self._channel(connection, channel_id)
            if current is not None and current.status != "active":
                raise HTTPException(409, "This channel has an unfinished deployment operation")
            if current is not None and (current.name != details.name or current.pool_id != details.pool_id):
                raise HTTPException(409, "The channel changed. Reload it before adding deployments")
            pool: Final = await self._pool(connection, details.pool_id)
            await self._validate_name(connection, details.name, channel_id)
            await self._validate_members(connection, channel_id, members)
            previous: Final = await self._members(connection, channel_id)
            selected: Final = (*previous, *members)
            row: Final = await self._save_row(
                connection, channel_id, details, "provisioning", tuple(item.deployment_id for item in members)
            )
            await self._replace_members(connection, row, selected, current)
            return channel_view(row, selected, pool.name)

    async def finish_new(
        self,
        channel_id: str,
        added_ids: Sequence[str],
        *,
        failed: bool,
        cleanup_failed: Sequence[str] = (),
        created: bool = False,
    ) -> dict[str, JsonValue]:
        async with self.wallet_store.transaction() as connection:
            await self.pools._settings(connection, lock=True)
            current: Final = await self._channel(connection, channel_id)
            if current is None:
                raise HTTPException(404, "Channel does not exist")
            previous: Final = await self._members(connection, channel_id)
            selected: Final = (
                tuple(
                    item
                    for item in previous
                    if item.deployment_id not in added_ids or item.deployment_id in cleanup_failed
                )
                if failed
                else previous
            )
            status: Final = "error" if cleanup_failed else "active"
            row: Final = await self._save_row(connection, channel_id, current, status, cleanup_failed)
            await self._replace_members(connection, row, selected, current)
            if failed and created and not cleanup_failed:
                await connection.execute("DELETE FROM retail_channels WHERE id=%s", (channel_id,))
            pool: Final = await self._pool(connection, current.pool_id)
            return channel_view(row, selected, pool.name)

    async def pending_operation(self, channel_id: str) -> tuple[str, ...]:
        async with self.wallet_store.transaction() as connection:
            await self.pools._settings(connection, lock=True)
            current: Final = await self._channel(connection, channel_id)
            if current is None:
                raise HTTPException(404, "Channel does not exist")
            if not current.pending_deployment_ids:
                raise HTTPException(409, "This channel has no pending deployment operation")
            return current.pending_deployment_ids

    async def _pool(self, connection: psycopg.AsyncConnection, pool_id: str) -> _PoolRow:
        row: Final = await _one(
            connection, "SELECT * FROM retail_resource_pools WHERE id=%s FOR UPDATE", (pool_id,), _PoolRow
        )
        if row is None:
            raise HTTPException(404, "Select an existing resource pool before saving a channel")
        return row

    async def _channel(self, connection: psycopg.AsyncConnection, channel_id: str) -> ChannelRow | None:
        return await _one(connection, "SELECT * FROM retail_channels WHERE id=%s FOR UPDATE", (channel_id,), ChannelRow)

    async def _members(self, connection: psycopg.AsyncConnection, channel_id: str) -> tuple[ChannelMember, ...]:
        return await _many(
            connection, "SELECT * FROM retail_channel_deployments WHERE channel_id=%s", (channel_id,), ChannelMember
        )

    async def _validate_name(self, connection: psycopg.AsyncConnection, name: str, channel_id: str) -> None:
        duplicate: Final = await _one(
            connection,
            "SELECT * FROM retail_channels WHERE lower(name)=lower(%s) AND id<>%s",
            (name, channel_id),
            ChannelRow,
        )
        if duplicate is not None:
            raise HTTPException(409, "Use a distinct channel name so operators can identify the upstream")

    async def _validate_members(
        self, connection: psycopg.AsyncConnection, channel_id: str, members: Sequence[ChannelMember]
    ) -> None:
        owners: Final = await _many(connection, "SELECT * FROM retail_channel_deployments", (), ChannelMember)
        selected: Final = frozenset(item.deployment_id for item in members)
        if any(item.deployment_id in selected and item.channel_id != channel_id for item in owners):
            raise HTTPException(409, "A deployment already belongs to another named channel")

    async def _save_row(
        self,
        connection: psycopg.AsyncConnection,
        channel_id: str,
        details: ChannelDetails,
        status: str,
        pending_ids: Sequence[str] = (),
    ) -> ChannelRow:
        row: Final = await _one(
            connection,
            """INSERT INTO retail_channels(id,name,pool_id,status,pending_deployment_ids) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,pool_id=EXCLUDED.pool_id,status=EXCLUDED.status,
            pending_deployment_ids=EXCLUDED.pending_deployment_ids,version=retail_channels.version+1,updated_at=now()
            RETURNING *""",
            (channel_id, details.name, details.pool_id, status, Jsonb(list(pending_ids))),
            ChannelRow,
        )
        if row is None:
            raise HTTPException(503, "Channel could not be saved")
        return row

    async def _replace_members(
        self,
        connection: psycopg.AsyncConnection,
        channel: ChannelRow,
        members: Sequence[ChannelMember],
        previous: ChannelRow | None,
    ) -> None:
        old: Final = await self._members(connection, channel.id)
        affected: Final = tuple({item.deployment_id for item in (*old, *members)})
        bindings: Final = await _many(
            connection,
            "SELECT * FROM retail_pool_bindings WHERE deployment_id=ANY(%s)",
            (list(affected),),
            _StoredBinding,
        )
        preserved: Final = {
            (item.deployment_id, item.model): item
            for item in bindings
            if item.pool_id == channel.pool_id and (previous is None or previous.pool_id == channel.pool_id)
        }
        await connection.execute("DELETE FROM retail_channel_deployments WHERE channel_id=%s", (channel.id,))
        await connection.execute("DELETE FROM retail_pool_bindings WHERE deployment_id=ANY(%s)", (list(affected),))
        for member in members:
            await connection.execute(
                "INSERT INTO retail_channel_deployments(channel_id,deployment_id,model) VALUES (%s,%s,%s)",
                (channel.id, member.deployment_id, member.model),
            )
            binding: Final = preserved.get((member.deployment_id, member.model))
            await connection.execute(
                "INSERT INTO retail_pool_bindings(pool_id,deployment_id,model,weight,priority) VALUES (%s,%s,%s,%s,%s)",
                (
                    channel.pool_id,
                    member.deployment_id,
                    member.model,
                    binding.weight if binding else 1,
                    binding.priority if binding else 0,
                ),
            )
        await connection.execute(
            "UPDATE retail_resource_pools SET version=version+1,updated_at=now() WHERE id=ANY(%s)",
            (list({channel.pool_id, *(item.pool_id for item in bindings)}),),
        )


async def validate_channel_bindings(
    connection: psycopg.AsyncConnection, pool_id: str, bindings: Sequence[PoolBinding]
) -> None:
    channels: Final = await _many(connection, "SELECT * FROM retail_channels", (), ChannelRow)
    members: Final = await _many(connection, "SELECT * FROM retail_channel_deployments", (), ChannelMember)
    require_whole_channels(channels, members, pool_id, bindings)


def require_whole_channels(
    channels: Sequence[ChannelRow], members: Sequence[ChannelMember], pool_id: str, bindings: Sequence[PoolBinding]
) -> None:
    owners: Final = {channel.id: channel for channel in channels}
    selected: Final = frozenset((binding.deployment_id, binding.model) for binding in bindings)
    for member in members:
        required: Final = owners[member.channel_id].pool_id == pool_id
        included: Final = (member.deployment_id, member.model) in selected
        if required != included:
            raise HTTPException(409, "Named channels are assigned as a whole. Change their resource pool in Channels")

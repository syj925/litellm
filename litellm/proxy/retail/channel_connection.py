import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from secrets import randbits
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter, field_validator

from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper, encrypt_value_helper, get_salt_key

from .channel_store import ChannelMember, ChannelRow, ChannelStore, ChannelUpdate
from .pool_store import _StoredBinding, router_deployments
from .pricing import as_mapping
from .store import WalletStore, _many


def valid_api_base(value: str) -> str:
    cleaned: Final = value.strip()
    try:
        parsed: Final = urlsplit(cleaned)
        port: Final = parsed.port
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in cleaned
            or "#" in cleaned
            or "\\" in cleaned
            or any(ord(char) < 33 or ord(char) == 127 for char in cleaned)
            or (port is not None and port < 1)
        ):
            raise ValueError("invalid")
        return cleaned
    except ValueError:
        raise ValueError("Use an HTTP or HTTPS API base without credentials, query parameters, or fragments") from None


class ChannelConnectionPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    api_base: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = Field(default=None, repr=False)

    @field_validator("api_base")
    @classmethod
    def checked_base(cls, value: str | None) -> str | None:
        return valid_api_base(value) if value is not None and value.strip() else None

    @field_validator("api_key")
    @classmethod
    def checked_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        raw: Final = value.get_secret_value().strip()
        if len(raw) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise ValueError("Use a valid API key without control characters")
        return SecretStr(raw)

    @property
    def changed(self) -> bool:
        return self.api_base is not None or self.api_key is not None


class ChannelConnectionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    version: int
    deployment_count: int
    editable_deployment_count: int
    can_edit: bool
    reason: str | None
    api_base: str | None
    api_base_mixed: bool
    has_api_key: bool | None
    credential_mode: Literal["direct", "referenced", "mixed", "unknown"]


class _EncryptedModel(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    model_id: str
    litellm_params: dict[str, JsonValue] = Field(repr=False)
    updated_by: str
    updated_at: datetime

    @field_validator("litellm_params", mode="before")
    @classmethod
    def json_params(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value


class _LockRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    acquired: bool


@dataclass(frozen=True, slots=True)
class ModelConnectionChange:
    original: _EncryptedModel
    params: Mapping[str, JsonValue]
    changed_fields: tuple[str, ...]


class ConnectionRuntime(Protocol):
    def lock(self) -> AbstractAsyncContextManager[None]: ...

    def deployments(self) -> Sequence[Mapping[str, object]]: ...

    async def same_database(self, key: int) -> bool: ...

    def encrypt(self, value: str) -> str: ...

    async def reconcile(self, changes: Sequence[ModelConnectionChange], *, restored: bool = False) -> None: ...

    async def publish(self) -> None: ...


_STRING: Final = TypeAdapter(str)
_LOCKS: Final = TypeAdapter(tuple[_LockRow, ...])
_NATIVE_AUTH_FIELDS: Final = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_web_identity_token",
        "aws_web_identity_token_file",
        "aws_profile_name",
        "aws_role_name",
        "aws_role_arn",
        "aws_external_id",
        "vertex_credentials",
        "vertex_credentials_path",
        "azure_ad_token",
        "azure_ad_token_provider",
        "oauth_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "google_credentials",
    }
)


def _decrypted(value: JsonValue, field: str) -> JsonValue:
    return (
        _STRING.validate_python(
            decrypt_value_helper(value, key=field, exception_type="debug", return_original_value=True)
        )
        if isinstance(value, str)
        else value
    )


def _decoded_params(row: _EncryptedModel) -> Mapping[str, JsonValue]:
    return {key: _decrypted(value, key) for key, value in row.litellm_params.items()}


def _current_safe_base(params: Mapping[str, JsonValue]) -> str | None:
    value: Final = params.get("api_base")
    if not isinstance(value, str) or not value:
        return None
    try:
        return valid_api_base(value)
    except ValueError:
        return None


def connection_summary(
    channel: ChannelRow,
    members: Sequence[ChannelMember],
    rows: Sequence[_EncryptedModel],
) -> ChannelConnectionSummary:
    decoded: Final = tuple(_decoded_params(row) for row in rows)
    refs: Final = tuple(bool(item.get("litellm_credential_name")) for item in decoded)
    editable: Final = sum(not item for item in refs)
    bases: Final = frozenset(_current_safe_base(item) for item in decoded)
    keys: Final = frozenset(bool(item.get("api_key")) for item in decoded)
    unsafe_base: Final = any(bool(item.get("api_base")) and _current_safe_base(item) is None for item in decoded)
    native_auth: Final = any(
        not item.get("api_key") and any(item.get(field) for field in _NATIVE_AUTH_FIELDS) for item in decoded
    )
    mode: Final = "unknown" if not rows else "referenced" if all(refs) else "mixed" if any(refs) else "direct"
    reason: Final = (
        "Add database-backed models before changing this channel connection"
        if not members
        else "Static or missing model deployments are read-only; store every selected model in the database first"
        if len(rows) != len(members)
        else "Named credentials are shared and read-only here. Manage them in LLM Credentials or use independent direct credentials"
        if any(refs)
        else "An existing API base uses an environment reference or sensitive URL components. Update it in model management before editing this channel connection"
        if unsafe_base
        else "These models use native cloud, OAuth, or token credentials instead of API keys. Manage their authentication in model management"
        if native_auth
        else "Recover the unfinished channel operation before changing its connection"
        if channel.status != "active"
        else None
    )
    return ChannelConnectionSummary(
        channel_id=channel.id,
        version=channel.version,
        deployment_count=len(members),
        editable_deployment_count=editable,
        can_edit=reason is None,
        reason=reason,
        api_base=next(iter(bases)) if len(bases) == 1 else None,
        api_base_mixed=len(bases) > 1,
        has_api_key=next(iter(keys)) if len(keys) == 1 and len(rows) == len(members) else None,
        credential_mode=mode,
    )


def connection_changes(
    rows: Sequence[_EncryptedModel],
    patch: ChannelConnectionPatch,
    encrypt: Callable[[str], str],
) -> tuple[ModelConnectionChange, ...]:
    fields: Final = tuple(name for name in ("api_base", "api_key") if getattr(patch, name) is not None)
    replacements: Final = {
        **({"api_base": encrypt(patch.api_base)} if patch.api_base is not None else {}),
        **({"api_key": encrypt(patch.api_key.get_secret_value())} if patch.api_key is not None else {}),
    }
    return tuple(ModelConnectionChange(row, {**row.litellm_params, **replacements}, fields) for row in rows)


class ProxyConnectionRuntime:
    def deployments(self) -> Sequence[Mapping[str, object]]:
        return router_deployments()

    @asynccontextmanager
    async def lock(self) -> AsyncIterator[None]:
        from litellm.proxy.proxy_server import MODEL_RECONCILE_LOCK, prisma_client, store_model_in_db

        if prisma_client is None or store_model_in_db is not True:
            raise HTTPException(409, "Enable database-backed model management before changing channel connections")
        async with MODEL_RECONCILE_LOCK:
            yield

    async def same_database(self, key: int) -> bool:
        from litellm.proxy.proxy_server import prisma_client

        if prisma_client is None:
            return False
        rows: Final = _LOCKS.validate_python(
            await prisma_client.writer_db.query_raw("SELECT pg_try_advisory_xact_lock($1::bigint) AS acquired", key)
        )
        return len(rows) == 1 and not rows[0].acquired

    def encrypt(self, value: str) -> str:
        if not get_salt_key():
            raise HTTPException(503, "Model encryption is not configured")
        try:
            return _STRING.validate_python(encrypt_value_helper(value))
        except Exception:
            raise HTTPException(503, "Model connection encryption failed") from None

    async def reconcile(self, changes: Sequence[ModelConnectionChange], *, restored: bool = False) -> None:
        from litellm.proxy.proxy_server import prisma_client, proxy_config, proxy_logging_obj

        if prisma_client is None:
            raise RuntimeError("Model storage is unavailable")
        outcome: Final = await proxy_config.add_deployment_locked(
            prisma_client=prisma_client, proxy_logging_obj=proxy_logging_obj
        )
        if outcome.still_desired is None or outcome.live_after is None:
            raise RuntimeError("Model reconciliation did not finish")
        live: Final = {str(as_mapping(item.get("model_info")).get("id")): item for item in router_deployments()}
        if not connection_changes_are_live(changes, live, restored=restored):
            raise RuntimeError("The new channel connection is not live")

    async def publish(self) -> None:
        from litellm.proxy.common_utils.config_sync_pubsub import publish_config_change_for_object_type

        await publish_config_change_for_object_type("litellm_proxymodeltable")


def connection_changes_are_live(
    changes: Sequence[ModelConnectionChange],
    live: Mapping[str, Mapping[str, object]],
    *,
    restored: bool = False,
) -> bool:
    def matches(change: ModelConnectionChange) -> bool:
        deployment: Final = live.get(change.original.model_id)
        if deployment is None:
            return False
        params: Final = as_mapping(deployment.get("litellm_params"))
        expected: Final = change.original.litellm_params if restored else change.params

        def equal(field: str) -> bool:
            actual: Final = params.get(field)
            return (actual.get_secret_value() if isinstance(actual, SecretStr) else actual) == _decrypted(
                expected.get(field), field
            )

        return all(equal(field) for field in change.changed_fields)

    return all(matches(change) for change in changes)


async def _model_rows(connection: psycopg.AsyncConnection, identifiers: Sequence[str]) -> tuple[_EncryptedModel, ...]:
    return await _many(
        connection,
        'SELECT model_id,litellm_params,updated_by,updated_at FROM "LiteLLM_ProxyModelTable" WHERE model_id=ANY(%s) ORDER BY model_id FOR UPDATE',
        (list(identifiers),),
        _EncryptedModel,
    )


async def get_channel_connection(store: WalletStore, channel_id: str) -> ChannelConnectionSummary:
    channels: Final = ChannelStore(store)
    async with store.transaction() as connection:
        channel: Final = await channels._channel(connection, channel_id)
        if channel is None:
            raise HTTPException(404, "Channel does not exist")
        members: Final = await channels._members(connection, channel_id)
        rows: Final = await _model_rows(connection, tuple(item.deployment_id for item in members))
        return connection_summary(channel, members, rows)


@dataclass(frozen=True, slots=True)
class _ConnectionSave:
    old_channel: ChannelRow
    old_members: tuple[ChannelMember, ...]
    old_bindings: tuple[_StoredBinding, ...]
    written_bindings: tuple[_StoredBinding, ...]
    affected_ids: tuple[str, ...]
    changes: tuple[ModelConnectionChange, ...]
    written_models: tuple[_EncryptedModel, ...]
    result: dict[str, JsonValue]


async def _bindings(connection: psycopg.AsyncConnection, identifiers: Sequence[str]) -> tuple[_StoredBinding, ...]:
    return await _many(
        connection,
        "SELECT * FROM retail_pool_bindings WHERE deployment_id=ANY(%s) ORDER BY pool_id,deployment_id,model",
        (list(identifiers),),
        _StoredBinding,
    )


async def _save_connection(
    channels: ChannelStore,
    channel_id: str,
    details: ChannelUpdate,
    patch: ChannelConnectionPatch,
    runtime: ConnectionRuntime,
    actor: str,
) -> _ConnectionSave:
    async with channels.wallet_store.transaction() as connection:
        await channels.pools._settings(connection, lock=True)
        current: Final = await channels._channel(connection, channel_id)
        if current is None:
            raise HTTPException(404, "Channel does not exist")
        if details.expected_version != current.version:
            raise HTTPException(409, "The channel changed. Reload it before saving your edits")
        challenge: Final = randbits(63)
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (challenge,))
        if not await runtime.same_database(challenge):
            raise HTTPException(
                409, "Channel connections require retail storage and native model storage in the same database"
            )
        old_members: Final = await channels._members(connection, channel_id)
        rows: Final = await _model_rows(connection, details.deployment_ids)
        selected: Final = tuple(
            ChannelMember(channel_id=channel_id, deployment_id=item, model="") for item in details.deployment_ids
        )
        summary: Final = connection_summary(current, selected, rows)
        if not summary.can_edit:
            raise HTTPException(409, summary.reason)
        affected: Final = tuple(sorted({*details.deployment_ids, *(item.deployment_id for item in old_members)}))
        old_bindings: Final = await _bindings(connection, affected)
        changes: Final = connection_changes(rows, patch, runtime.encrypt)
        result: Final = await channels.save_existing_in_transaction(
            connection, details, runtime.deployments(), channel_id
        )
        for change in changes:
            await connection.execute(
                'UPDATE "LiteLLM_ProxyModelTable" SET litellm_params=%s,updated_by=%s,updated_at=now() WHERE model_id=%s',
                (Jsonb(dict(change.params)), actor, change.original.model_id),
            )
        written: Final = await _bindings(connection, affected)
        written_models: Final = await _model_rows(connection, details.deployment_ids)
        return _ConnectionSave(current, old_members, old_bindings, written, affected, changes, written_models, result)


async def _restore_connection(channels: ChannelStore, saved: _ConnectionSave) -> None:
    async with channels.wallet_store.transaction() as connection:
        await channels.pools._settings(connection, lock=True)
        current: Final = await channels._channel(connection, saved.old_channel.id)
        live_rows: Final = await _model_rows(connection, tuple(item.original.model_id for item in saved.changes))
        current_bindings: Final = await _bindings(connection, saved.affected_ids)
        if (
            current is None
            or current.version != saved.old_channel.version + 1
            or live_rows != saved.written_models
            or current_bindings != saved.written_bindings
        ):
            raise RuntimeError("Concurrent model changes prevent an automatic rollback")
        for change in saved.changes:
            await connection.execute(
                'UPDATE "LiteLLM_ProxyModelTable" SET litellm_params=%s,updated_by=%s,updated_at=now() WHERE model_id=%s',
                (Jsonb(change.original.litellm_params), change.original.updated_by, change.original.model_id),
            )
        await connection.execute(
            "UPDATE retail_channels SET name=%s,pool_id=%s,status=%s,version=version+1,updated_at=now() WHERE id=%s",
            (saved.old_channel.name, saved.old_channel.pool_id, saved.old_channel.status, saved.old_channel.id),
        )
        await connection.execute("DELETE FROM retail_channel_deployments WHERE channel_id=%s", (saved.old_channel.id,))
        for member in saved.old_members:
            await connection.execute(
                "INSERT INTO retail_channel_deployments(channel_id,deployment_id,model) VALUES (%s,%s,%s)",
                (member.channel_id, member.deployment_id, member.model),
            )
        await connection.execute(
            "DELETE FROM retail_pool_bindings WHERE deployment_id=ANY(%s)", (list(saved.affected_ids),)
        )
        for binding in saved.old_bindings:
            await connection.execute(
                "INSERT INTO retail_pool_bindings(pool_id,deployment_id,model,weight,priority) VALUES (%s,%s,%s,%s,%s)",
                (binding.pool_id, binding.deployment_id, binding.model, binding.weight, binding.priority),
            )
        pools: Final = tuple(
            {
                saved.old_channel.pool_id,
                *(item.pool_id for item in saved.written_bindings),
                *(item.pool_id for item in saved.old_bindings),
            }
        )
        await connection.execute(
            "UPDATE retail_resource_pools SET version=version+1,updated_at=now() WHERE id=ANY(%s)", (list(pools),)
        )


async def apply_channel_connection(
    store: WalletStore,
    channel_id: str,
    details: ChannelUpdate,
    patch: ChannelConnectionPatch,
    actor: str,
    runtime: ConnectionRuntime,
) -> dict[str, JsonValue]:
    channels: Final = ChannelStore(store)
    async with channels.operation_lock(channel_id):
        async with runtime.lock():
            saved: Final = await _save_connection(channels, channel_id, details, patch, runtime, actor)
            try:
                await runtime.reconcile(saved.changes)
                await runtime.publish()
                return saved.result
            except BaseException as original:

                async def restore() -> None:
                    await _restore_connection(channels, saved)
                    await runtime.reconcile(saved.changes, restored=True)
                    await runtime.publish()

                try:
                    await asyncio.shield(restore())
                except BaseException:
                    try:
                        await runtime.publish()
                    except Exception:
                        pass
                    raise HTTPException(
                        503,
                        "Connection refresh failed and automatic recovery could not finish. Administrator review is required",
                    ) from None
                if not isinstance(original, Exception):
                    raise
                raise HTTPException(
                    503,
                    "Connection refresh failed. Channel settings and all stored connections were restored; reload before retrying",
                ) from None

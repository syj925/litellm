import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, Protocol, TypeAlias

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

ObjectKind: TypeAlias = Literal["key", "team", "organization", "model"]
NativeKind: TypeAlias = Literal["key", "team", "model"]


class NativeTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    native_id: str = Field(repr=False)
    name: str | None = None
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    owner_id: str | None = Field(default=None, repr=False)
    team_id: str | None = Field(default=None, repr=False)

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


@dataclass(frozen=True, slots=True)
class NativeTargetPage:
    items: tuple[NativeTarget, ...]
    total: int


class NativeTargetReader(Protocol):
    async def list(self, kind: NativeKind, q: str, limit: int, offset: int) -> NativeTargetPage: ...

    async def get(self, kind: NativeKind, native_id: str) -> NativeTarget | None: ...


class _NativeTransaction(Protocol):
    async def execute_raw(self, query: str, *args: object) -> object: ...

    async def query_raw(self, query: str, *args: object) -> object: ...


class _NativeClient(Protocol):
    def tx(self, *, timeout: timedelta) -> AbstractAsyncContextManager[_NativeTransaction]: ...


class _Count(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int


_TARGETS: Final = TypeAdapter(tuple[NativeTarget, ...])
_COUNTS: Final = TypeAdapter(tuple[_Count, ...])
_PRIVATE_NAME: Final = re.compile(r"sk-|bearer\s|https?://|(?:api[_-]?key|token)\s*[:=]|[0-9a-f]{64}", re.IGNORECASE)
_NATIVE_READ_SLOTS: Final = asyncio.Semaphore(2)


def safe_display_name(value: str | None, fallback: str) -> str:
    if not value or len(value) > 120 or any(ord(char) < 32 for char in value) or _PRIVATE_NAME.search(value):
        return fallback
    return value.strip() or fallback


def safe_actor_name(value: str | None) -> str | None:
    return safe_display_name(value, "") or None


def native_projection(kind: NativeKind) -> tuple[str, str, str, str]:
    match kind:
        case "key":
            return (
                '"LiteLLM_VerificationToken"',
                "token",
                "key_alias",
                (
                    "token AS native_id,key_alias AS name,created_at,NULL::text AS created_by,"
                    "updated_at,NULL::text AS updated_by,user_id AS owner_id,team_id"
                ),
            )
        case "team":
            return (
                '"LiteLLM_TeamTable"',
                "team_id",
                "team_alias",
                (
                    "team_id AS native_id,team_alias AS name,created_at,NULL::text AS created_by,"
                    "updated_at,NULL::text AS updated_by"
                ),
            )
        case "model":
            return (
                '"LiteLLM_ProxyModelTable"',
                "model_id",
                "model_name",
                "model_id AS native_id,model_name AS name,created_at,created_by,updated_at,updated_by",
            )


class PrismaTargetReader:
    def __init__(self, client: _NativeClient) -> None:
        self._client = client

    async def list(self, kind: NativeKind, q: str, limit: int, offset: int) -> NativeTargetPage:
        table, identity, name, projection = native_projection(kind)
        condition: Final = " WHERE created_at IS NOT NULL AND COALESCE(" + name + ",'') ILIKE $1"
        pattern: Final = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self._client.tx(timeout=timedelta(seconds=3)) as connection:
            await connection.execute_raw("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await connection.execute_raw("SET LOCAL statement_timeout='1500ms'")
            counts: Final = _COUNTS.validate_python(
                await connection.query_raw("SELECT count(*) AS total FROM " + table + condition, pattern)
            )
            rows: Final = _TARGETS.validate_python(
                await connection.query_raw(
                    "SELECT "
                    + projection
                    + " FROM "
                    + table
                    + condition
                    + " ORDER BY created_at DESC,"
                    + identity
                    + " LIMIT $2 OFFSET $3",
                    pattern,
                    limit,
                    offset,
                )
            )
        return NativeTargetPage(rows, counts[0].total if counts else 0)

    async def get(self, kind: NativeKind, native_id: str) -> NativeTarget | None:
        table, identity, _, projection = native_projection(kind)
        async with self._client.tx(timeout=timedelta(seconds=3)) as connection:
            await connection.execute_raw("SET TRANSACTION READ ONLY")
            await connection.execute_raw("SET LOCAL statement_timeout='1500ms'")
            rows: Final = _TARGETS.validate_python(
                await connection.query_raw(
                    "SELECT " + projection + " FROM " + table + " WHERE " + identity + "=$1 AND created_at IS NOT NULL",
                    native_id,
                )
            )
        return rows[0] if rows else None


def native_target_reader() -> NativeTargetReader:
    from litellm.proxy import proxy_server  # noqa: PLC0415  # lazy runtime binding avoids proxy initialization cycles

    client: Final[_NativeClient | None] = proxy_server.prisma_client
    if client is None:
        raise HTTPException(503, "Object authority is temporarily unavailable")
    return PrismaTargetReader(client)


async def read_native_page(
    reader: NativeTargetReader, kind: NativeKind, q: str, limit: int, offset: int
) -> NativeTargetPage:
    try:
        async with _native_slot(), asyncio.timeout(3):
            return await reader.list(kind, q, limit, offset)
    except Exception as exc:
        raise HTTPException(503, "Object authority is temporarily unavailable") from exc


async def read_native_target(reader: NativeTargetReader, kind: NativeKind, native_id: str) -> NativeTarget | None:
    try:
        async with _native_slot(), asyncio.timeout(3):
            return await reader.get(kind, native_id)
    except Exception as exc:
        raise HTTPException(503, "Object authority is temporarily unavailable") from exc


@asynccontextmanager
async def _native_slot() -> AsyncIterator[None]:
    async with asyncio.timeout(0.1):
        await _NATIVE_READ_SLOTS.acquire()
    try:
        yield
    finally:
        _NATIVE_READ_SLOTS.release()

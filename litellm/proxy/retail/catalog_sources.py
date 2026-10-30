import asyncio
import hashlib
import io
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from itertools import chain
from typing import Final, Literal

import httpx
import psycopg
from fastapi import APIRouter, FastAPI, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from .api import Admin, Store
from .catalog_source_data import (
    ExternalMetadataRecord,
    SourceName,
    normalize_models_dev,
    normalize_openrouter,
)
from .store import WalletStore, _many, _one, get_store

REFRESH_SECONDS: Final = 21600
_MAX_BYTES: Final = 32 * 1024 * 1024
_JSON: Final = TypeAdapter(dict[str, JsonValue])
_logger: Final = logging.getLogger(__name__)
_SOURCES: Final[tuple[SourceName, ...]] = ("models_dev", "openrouter")
_URLS: Final = {
    "models_dev": "https://models.dev/models.json",
    "openrouter": "https://openrouter.ai/api/v1/models",
}
_LABELS: Final = {"models_dev": "models.dev", "openrouter": "OpenRouter"}


class CatalogSourceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    label: str
    url: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    record_count: int = 0
    last_error: str | None = None
    stale: bool = True


class CatalogSourcesPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    sources: tuple[CatalogSourceStatus, ...]
    syncing: bool = False
    refresh_interval_seconds: int = REFRESH_SECONDS


class SourceFetchSuccess(BaseModel):
    model_config = ConfigDict(frozen=True)

    records: tuple[ExternalMetadataRecord, ...]


class SourceFetchFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: Literal[
        "The source could not be reached",
        "The source returned invalid metadata",
        "The source response exceeded the size limit",
        "The source returned an unexpectedly small catalog",
    ]


class _Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    records: tuple[ExternalMetadataRecord, ...] = ()
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


class _Lock(BaseModel):
    model_config = ConfigDict(frozen=True)

    acquired: bool


async def _download(
    client: httpx.AsyncClient, url: str, max_bytes: int = _MAX_BYTES
) -> dict[str, JsonValue] | SourceFetchFailure:
    try:
        async with asyncio.timeout(45):
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code != 200:
                    return SourceFetchFailure(error="The source could not be reached")
                with io.BytesIO() as body:
                    async for chunk in response.aiter_bytes():
                        if body.tell() + len(chunk) > max_bytes:
                            return SourceFetchFailure(error="The source response exceeded the size limit")
                        body.write(chunk)
                    return _JSON.validate_json(body.getvalue())
    except (httpx.HTTPError, TimeoutError):
        return SourceFetchFailure(error="The source could not be reached")
    except (ValidationError, ValueError):
        return SourceFetchFailure(error="The source returned invalid metadata")


async def fetch_source(
    client: httpx.AsyncClient, source: SourceName, retrieved_at: datetime
) -> SourceFetchSuccess | SourceFetchFailure:
    if source == "models_dev":
        models, providers = await asyncio.gather(
            _download(client, _URLS[source]), _download(client, "https://models.dev/api.json")
        )
        if isinstance(models, SourceFetchFailure):
            return models
        if isinstance(providers, SourceFetchFailure):
            return providers
        records: Final = normalize_models_dev(models, providers, retrieved_at)
        return (
            SourceFetchSuccess(records=records)
            if records
            else SourceFetchFailure(error="The source returned invalid metadata")
        )
    document: Final = await _download(client, _URLS[source])
    if isinstance(document, SourceFetchFailure):
        return document
    entries: Final = normalize_openrouter(document, retrieved_at)
    return (
        SourceFetchSuccess(records=entries)
        if entries
        else SourceFetchFailure(error="The source returned invalid metadata")
    )


def retained_snapshot(
    previous: _Snapshot, fetched: SourceFetchSuccess | SourceFetchFailure, attempted_at: datetime
) -> _Snapshot:
    if isinstance(fetched, SourceFetchFailure):
        return previous.model_copy(update={"last_attempt_at": attempted_at, "last_error": fetched.error})
    if len(previous.records) >= 20 and len(fetched.records) * 2 < len(previous.records):
        return previous.model_copy(
            update={"last_attempt_at": attempted_at, "last_error": "The source returned an unexpectedly small catalog"}
        )
    return _Snapshot(
        source=previous.source,
        records=fetched.records,
        last_attempt_at=attempted_at,
        last_success_at=attempted_at,
    )


def _status(snapshot: _Snapshot, now: datetime) -> CatalogSourceStatus:
    return CatalogSourceStatus(
        source=snapshot.source,
        label=_LABELS[snapshot.source],
        url=_URLS[snapshot.source],
        last_attempt_at=snapshot.last_attempt_at,
        last_success_at=snapshot.last_success_at,
        record_count=len(snapshot.records),
        last_error=snapshot.last_error,
        stale=snapshot.last_success_at is None
        or now - snapshot.last_success_at > timedelta(seconds=REFRESH_SECONDS * 2),
    )


async def _snapshots(connection: psycopg.AsyncConnection) -> tuple[_Snapshot, ...]:
    stored: Final = await _many(
        connection,
        "SELECT source,records,last_attempt_at,last_success_at,last_error FROM retail_catalog_sources ORDER BY source",
        (),
        _Snapshot,
    )
    by_source: Final = {snapshot.source: snapshot for snapshot in stored}
    return tuple(by_source.get(source, _Snapshot(source=source)) for source in _SOURCES)


async def read_records(store: WalletStore) -> tuple[ExternalMetadataRecord, ...]:
    async with store.transaction() as connection:
        snapshots: Final = await _snapshots(connection)
    return tuple(chain.from_iterable(snapshot.records for snapshot in snapshots))


async def source_status(store: WalletStore) -> CatalogSourcesPage:
    async with store.transaction() as connection:
        snapshots: Final = await _snapshots(connection)
    now: Final = datetime.now(timezone.utc)
    return CatalogSourcesPage(sources=tuple(_status(snapshot, now) for snapshot in snapshots))


def _is_due(snapshots: tuple[_Snapshot, ...], now: datetime) -> bool:
    return any(
        snapshot.last_attempt_at is None or now - snapshot.last_attempt_at >= timedelta(seconds=REFRESH_SECONDS)
        for snapshot in snapshots
    )


async def _save(connection: psycopg.AsyncConnection, snapshot: _Snapshot) -> None:
    serialized: Final = tuple(record.model_dump(mode="json") for record in snapshot.records)
    digest: Final = hashlib.sha256(json.dumps(serialized, sort_keys=True).encode()).hexdigest()
    await connection.execute(
        """
        INSERT INTO retail_catalog_sources(source,records,last_attempt_at,last_success_at,last_error,content_sha256)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT(source) DO UPDATE SET records=EXCLUDED.records,last_attempt_at=EXCLUDED.last_attempt_at,
            last_success_at=EXCLUDED.last_success_at,last_error=EXCLUDED.last_error,content_sha256=EXCLUDED.content_sha256
        """,
        (
            snapshot.source,
            Jsonb(serialized),
            snapshot.last_attempt_at,
            snapshot.last_success_at,
            snapshot.last_error,
            digest,
        ),
    )


async def sync_sources(
    store: WalletStore, *, force: bool = True, transport: httpx.AsyncBaseTransport | None = None
) -> CatalogSourcesPage:
    async with store.transaction() as connection:
        lock: Final = await _one(
            connection,
            "SELECT pg_try_advisory_xact_lock(hashtextextended('retail:catalog-sources',0)) AS acquired",
            (),
            _Lock,
        )
        previous: Final = await _snapshots(connection)
        now: Final = datetime.now(timezone.utc)
        if lock is None or not lock.acquired:
            return CatalogSourcesPage(sources=tuple(_status(item, now) for item in previous), syncing=True)
        if not force and not _is_due(previous, now):
            return CatalogSourcesPage(sources=tuple(_status(item, now) for item in previous))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "LiteLLM-model-catalog"},
        ) as client:
            fetched: Final = await asyncio.gather(*(fetch_source(client, source, now) for source in _SOURCES))
        snapshots: Final = tuple(
            retained_snapshot(old, fresh, now) for old, fresh in zip(previous, fetched, strict=True)
        )
        for snapshot in snapshots:
            await _save(connection, snapshot)
        return CatalogSourcesPage(sources=tuple(_status(item, now) for item in snapshots))


async def _refresh_loop() -> None:
    while True:
        try:
            store: Final = get_store()
            await store.initialize()
            await sync_sources(store, force=False)
        except (psycopg.Error, RuntimeError, ValidationError):
            _logger.warning("Model catalog metadata refresh was unavailable")
        await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def catalog_sources_lifespan(app: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("RETAIL_CATALOG_SYNC_ENABLED", "true").lower() not in {"true", "1", "yes"}:
        yield
        return
    task: Final = asyncio.create_task(_refresh_loop(), name="retail-catalog-metadata")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


router: Final = APIRouter(tags=["Catalog sources"])


@router.get("/admin/catalog-sources", response_model=CatalogSourcesPage)
async def get_sources(admin: Admin, store: Store) -> CatalogSourcesPage:
    try:
        return await source_status(store)
    except psycopg.Error as exc:
        raise HTTPException(503, "Catalog source status is temporarily unavailable") from exc


@router.post("/admin/catalog-sources/sync", response_model=CatalogSourcesPage)
async def refresh_sources(admin: Admin, store: Store) -> CatalogSourcesPage:
    try:
        return await sync_sources(store)
    except psycopg.Error as exc:
        raise HTTPException(503, "Catalog source refresh is temporarily unavailable") from exc

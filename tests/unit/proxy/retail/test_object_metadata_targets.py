from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest

from litellm.proxy.retail.object_metadata_targets import (
    NativeKind,
    PrismaTargetReader,
    safe_actor_name,
    safe_display_name,
)


@dataclass(frozen=True, slots=True)
class NativeTransaction:
    async def execute_raw(self, query: str, *args: object) -> object:
        assert query in (
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            "SET TRANSACTION READ ONLY",
            "SET LOCAL statement_timeout='1500ms'",
        )
        return 0

    async def query_raw(self, query: str, *args: object) -> object:
        assert "litellm_params" not in query and "metadata" not in query and "key_name" not in query
        if "count(*)" in query:
            return [{"total": 1}]
        assert args
        return [
            {
                "native_id": "private-identity",
                "name": "Example",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
        ]


@dataclass(frozen=True, slots=True)
class NativeClient:
    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[NativeTransaction]:
        assert timeout == timedelta(seconds=3)
        yield NativeTransaction()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("key", "team", "model"))
async def test_native_target_reader_uses_private_minimal_projections_in_bounded_readonly_transaction(
    kind: NativeKind,
) -> None:
    reader: Final = PrismaTargetReader(NativeClient())
    page: Final = await reader.list(kind, "example", 25, 0)
    row: Final = await reader.get(kind, "private-identity")
    assert page.total == 1 and len(page.items) == 1
    assert row == page.items[0]
    assert "private-identity" not in repr(row)


@pytest.mark.parametrize(
    "value", ("sk-secret-fragment", "a" * 64, "Name\nsecret", "https://private.example", "token=hidden")
)
def test_native_alias_or_actor_secret_shapes_are_not_projected(value: str) -> None:
    assert safe_display_name(value, "Unnamed key") == "Unnamed key"
    assert safe_actor_name(value) is None


def test_safe_native_display_values_are_bounded_and_do_not_invent_provenance() -> None:
    assert safe_display_name("Production team", "Unnamed team") == "Production team"
    assert safe_display_name(None, "Unnamed key") == "Unnamed key"
    assert safe_actor_name(None) is None

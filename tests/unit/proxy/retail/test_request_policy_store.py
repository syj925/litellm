from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import pytest
from prisma.errors import PrismaError
from pydantic import JsonValue

from litellm.proxy.retail.request_policy_management import PolicyActor, PolicyCreate
from litellm.proxy.retail.request_policy_store import NativeRequestPolicyStore
from litellm.proxy.retail.request_policy_types import Definition, RequestPolicyFailure, ResolvedRequestPolicies


class Reader:
    def __init__(self, response: object) -> None:
        self.response = response
        self.queries: tuple[str, ...] = ()

    async def query_raw(self, query: str, *args: object) -> object:
        self.queries = (*self.queries, query)
        return self.response

    async def execute_raw(self, query: str, *args: object) -> int:
        raise PrismaError("private-native-failure")


@dataclass(frozen=True, slots=True)
class Client:
    writer_db: Reader

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[Reader]:
        yield self.writer_db


def response(*, active: bool = True) -> tuple[Mapping[str, JsonValue], ...]:
    return ({"key_data": {"token": "a"*64,"user_id": "fresh-owner", "models": ["allowed-model"],
        "max_budget": 7,"metadata": {"team_scope": "retained"}},
        "user_data": {"user_id": "fresh-owner", "user_role": "customer","models": ["allowed-model"]},
        "active": active,"rules": []},)


@pytest.mark.asyncio
async def test_runtime_lookup_has_exactly_one_writer_query_and_preserves_native_grants() -> None:
    writer: Final = Reader(response())
    store: Final = NativeRequestPolicyStore(Client(writer))
    result: Final = await store.lookup("fresh-owner", "a"*64, "allowed-model")
    assert isinstance(result, ResolvedRequestPolicies)
    assert len(writer.queries) == 1
    assert result.auth.api_key == "a"*64 and result.auth.token == "a"*64
    assert result.auth.models == ["allowed-model"] and result.auth.max_budget == 7
    assert result.auth.metadata == {"team_scope": "retained"}
    assert result.user is not None and result.user.user_role == "customer"


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,expected", (([], "forbidden"), (response(active=False), "forbidden"),
    ([{"unrecognized": "private-diagnostic-data"}], "unavailable")))
async def test_missing_revoked_and_malformed_authority_fail_closed(rows: object, expected: str) -> None:
    store: Final = NativeRequestPolicyStore(Client(Reader(rows)))
    failure: Final = await store.lookup("fresh-owner", "a"*64, "allowed-model")
    assert failure == RequestPolicyFailure(expected)
    assert "private-diagnostic-data" not in repr(failure)


@pytest.mark.asyncio
async def test_closed_store_does_not_query_or_close_borrowed_native_client() -> None:
    writer: Final = Reader(response())
    store: Final = NativeRequestPolicyStore(Client(writer))
    await store.close()
    assert await store.lookup("fresh-owner", "a"*64, "allowed-model") == RequestPolicyFailure("unavailable")
    assert not writer.queries


@pytest.mark.asyncio
async def test_management_storage_failure_is_tagged_without_native_error_details() -> None:
    store: Final = NativeRequestPolicyStore(Client(Reader([])))
    result: Final = await store.write(PolicyActor("owner", "a"*64), "policies", "created", "operation",
        PolicyCreate(name="Safe", definition=Definition()))
    assert result == RequestPolicyFailure("unavailable")
    assert "private-native-failure" not in repr(result)

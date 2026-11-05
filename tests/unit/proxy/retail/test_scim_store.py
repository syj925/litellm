from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

import pytest
from pydantic import SecretStr

from litellm.proxy.retail.native_audit_store import NativeAuditTransaction
from litellm.proxy.retail.scim_config import ScimConfig, ScimSetup
from litellm.proxy.retail.scim_protocol import ScimFailure, ScimList, ScimProfile, ScimQuery, ScimRecord
from litellm.proxy.retail.scim_store import NativeScimStore


@dataclass(frozen=True, slots=True)
class UnavailableWriter:
    async def query_raw(self, query: str, *args: object) -> object:
        raise TimeoutError("private-directory-body-and-credential")

    async def execute_raw(self, query: str, *args: object) -> int:
        raise TimeoutError("private-directory-body-and-credential")


@dataclass(frozen=True, slots=True)
class UnavailableClient:
    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[UnavailableWriter]:
        yield UnavailableWriter()


@dataclass(frozen=True, slots=True)
class ForbiddenClient:
    def tx(self, *, timeout: timedelta) -> AbstractAsyncContextManager[NativeAuditTransaction]:
        raise AssertionError("A closed store must not access the borrowed native client")


def setup() -> ScimSetup:
    return ScimSetup(ScimConfig(namespace="fixture-directory", generation=1, public_origin="https://gateway.invalid"),
        SecretStr("rt-scim." + "f" * 32))


async def requested(
    store: NativeScimStore, configuration: ScimSetup, operation: str,
) -> ScimFailure | ScimList | ScimRecord | None:
    identifier: Final = uuid4()
    match operation:
        case "initialize":
            return await store.initialize()
        case "configure":
            return await store.configure(configuration)
        case "list":
            return await store.list_users(configuration, ScimQuery())
        case "get":
            return await store.get_user(configuration, identifier)
        case "create":
            return await store.create_user(configuration,
                ScimProfile(external_id="fixture-external", user_name="fixture-user", active=True))
        case "update":
            return await store.update_user(configuration, identifier, b"{}", replace=False, if_match=None)
        case "delete":
            return await store.delete_user(configuration, identifier, None)
    raise AssertionError("Unknown test operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("initialize", "configure", "list", "get", "create", "update", "delete"))
@pytest.mark.parametrize("closed", (False, True))
async def test_borrowed_writer_outage_and_closed_admission_return_private_failures(operation: str, closed: bool) -> None:
    store: Final = NativeScimStore(ForbiddenClient() if closed else UnavailableClient())
    if closed:
        await store.close()
    result: Final = await requested(store, setup(), operation)
    assert result == ScimFailure(503)
    assert "private-directory" not in repr(result) and "rt-scim" not in repr(result)

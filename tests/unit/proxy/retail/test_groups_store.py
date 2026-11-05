from typing import Final
from uuid import uuid4

import pytest

from litellm.proxy.retail.scim_groups_protocol import GroupList, GroupProfile, GroupQuery, GroupRecord
from litellm.proxy.retail.scim_protocol import ScimFailure
from litellm.proxy.retail.scim_store import NativeScimStore
from tests.unit.proxy.retail.test_scim_store import ForbiddenClient, UnavailableClient, setup


async def requested(store: NativeScimStore, operation: str) -> GroupList | GroupRecord | ScimFailure | None:
    configuration: Final = setup()
    identifier: Final = uuid4()
    match operation:
        case "list":
            return await store.list_groups(configuration, GroupQuery())
        case "get":
            return await store.get_group(configuration, identifier)
        case "create":
            return await store.create_group(configuration, GroupProfile(external_id="fixture", display_name="Fixture"))
        case "update":
            return await store.update_group(configuration, identifier, b"{}", replace=False, if_match=None)
        case "delete":
            return await store.delete_group(configuration, identifier, None)
    raise AssertionError("Unknown test operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("list", "get", "create", "update", "delete"))
@pytest.mark.parametrize("closed", (False, True))
async def test_group_authority_outage_and_closed_admission_never_touch_borrowed_native_identity(
    operation: str, closed: bool,
) -> None:
    store: Final = NativeScimStore(ForbiddenClient() if closed else UnavailableClient())
    if closed:
        await store.close()
    result: Final = await requested(store, operation)
    assert result == ScimFailure(503) and "private-directory" not in repr(result)

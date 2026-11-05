import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.proxy._types import hash_token
from litellm.proxy.retail.scim_config import ScimSetup
from litellm.proxy.retail.scim_groups_protocol import (
    GROUP_SCHEMA,
    GroupList,
    GroupProfile,
    GroupQuery,
    GroupRecord,
    group_etag,
)
from litellm.proxy.retail.scim_protocol import ScimFailure, ScimRecord
from litellm.proxy.retail.scim_store import NativeScimStore
from tests.integration.database.test_retail_scim_store import Fixture, Writer, created, issued, native, patch

__all__ = ["native"]


def profile(label: str = "one", members: tuple[UUID, ...] = ()) -> GroupProfile:
    return GroupProfile(external_id="group-"+label, display_name="Group "+label, members=members)


async def group(native: Fixture, label: str = "one", members: tuple[UUID, ...] = ()) -> GroupRecord:
    result: Final = await native.store.create_group(native.setup, profile(label, members))
    assert isinstance(result, GroupRecord)
    return result


async def configured(native: Fixture, changes: Mapping[str, int | tuple[str, ...]]) -> Fixture:
    setup: Final = ScimSetup(native.setup.config.model_copy(update={**changes, "generation": native.setup.config.generation+1}), native.setup.token)
    assert await native.store.configure(setup) is None
    return Fixture(native.client, native.store, setup)


async def revision(native: Fixture, user: ScimRecord) -> UUID:
    result: Final = await native.store.get_user(native.setup, user.id)
    assert isinstance(result, ScimRecord)
    return result.credential_revision


@pytest.mark.asyncio
async def test_group_crud_is_directory_only_and_resolves_only_live_scim_user_resources(native: Fixture) -> None:
    user: Final = await created(native)
    current: Final = await group(native, members=(user.id,))
    assert current.members == (user.id,) and user.user_id not in repr(current)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        counts: Final = await writer.query_raw('''SELECT (SELECT count(*) FROM "LiteLLM_VerificationToken") AS keys,
            (SELECT count(*) FROM retail_wallets) AS wallets,(SELECT count(*) FROM "LiteLLM_UserTable") AS users''')
    assert counts == [{"keys": 0, "wallets": 1, "users": 2}]
    assert await native.store.create_group(native.setup, profile("bad", (uuid4(),))) == ScimFailure(400, "invalidValue")
    assert await native.store.create_group(native.setup, profile(members=())) == ScimFailure(409, "uniqueness")
    assert await native.store.get_group(native.setup, current.id) == current
    assert await native.store.delete_group(native.setup, current.id, group_etag(current)) is None
    assert await native.store.get_group(native.setup, current.id) == ScimFailure(404)
    replacement: Final = await group(native, members=(user.id,))
    assert replacement.id != current.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("deleted", "foreign", "native", "group"))
async def test_members_cannot_adopt_native_accounts_other_namespaces_deleted_users_or_groups(native: Fixture, kind: str) -> None:
    user: Final = await created(native)
    existing: Final = await group(native)
    if kind == "deleted":
        assert await native.store.delete_user(native.setup, user.id, None) is None
        member: Final = user.id
    elif kind == "foreign":
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.query_raw("UPDATE retail_scim_users SET namespace='foreign' WHERE id=$1::uuid RETURNING id", str(user.id))
        member: Final = user.id
    elif kind == "group":
        member: Final = existing.id
    else:
        member: Final = uuid4()
        async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
            await writer.query_raw('INSERT INTO "LiteLLM_UserTable"(user_id,user_role,models) VALUES($1,\'customer\',\'{}\') RETURNING user_id', str(member))
    assert await native.store.create_group(native.setup, profile("invalid", (member,))) == ScimFailure(400, "invalidValue")
    listing: Final = await native.store.list_groups(native.setup, GroupQuery())
    assert isinstance(listing, GroupList) and listing.resources == (existing,)


@pytest.mark.asyncio
async def test_ordered_remove_then_readd_rotates_prior_proof_even_when_final_membership_is_unchanged(native: Fixture) -> None:
    user: Final = await created(native)
    token: Final = await issued(native, user)
    current: Final = await group(native, members=(user.id,))
    body: Final = patch({"op": "remove", "path": f'members[value eq "{user.id}"]'},
        {"op": "add", "path": "members", "value": [{"value": str(user.id)}]})
    result: Final = await native.store.update_group(native.setup, current.id, body, replace=False, if_match=group_etag(current))
    assert isinstance(result, GroupRecord) and result.members == current.members and result.version == current.version+1
    assert await revision(native, user) != user.credential_revision
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw("SELECT credential_revision FROM retail_scim_credentials WHERE token_hash=$1", token)
    assert rows == [{"credential_revision": user.credential_revision}]


@pytest.mark.asyncio
async def test_noop_membership_order_and_stale_etag_preserve_versions_and_atomic_patch_errors(native: Fixture) -> None:
    first: Final = await created(native)
    second: Final = await created(native, "two")
    current: Final = await group(native, members=(first.id, second.id))
    body: Final = json.dumps({"schemas": [GROUP_SCHEMA], "externalId": current.external_id,
        "displayName": current.display_name, "members": [{"value": str(value)} for value in reversed(current.members)]}).encode()
    assert await native.store.update_group(native.setup, current.id, body, replace=True, if_match=group_etag(current)) == current
    bad: Final = patch({"op": "remove", "path": "members"}, {"op": "replace", "path": "externalId", "value": "changed"})
    assert isinstance(await native.store.update_group(native.setup, current.id, bad, replace=False, if_match=None), ScimFailure)
    assert await native.store.get_group(native.setup, current.id) == current
    assert await revision(native, first) == first.credential_revision
    bodies: Final = (patch({"op": "replace", "path": "displayName", "value": "First"}), patch({"op": "replace", "path": "displayName", "value": "Second"}))
    outcomes: Final = await asyncio.gather(*(native.store.update_group(native.setup, current.id, value, replace=False, if_match=group_etag(current)) for value in bodies))
    assert sum(isinstance(row, GroupRecord) for row in outcomes) == 1 and outcomes.count(ScimFailure(412)) == 1


@pytest.mark.asyncio
async def test_group_delete_rotates_all_old_members_and_user_delete_cleans_members_with_group_version(native: Fixture) -> None:
    first: Final = await created(native)
    second: Final = await created(native, "two")
    current: Final = await group(native, members=(first.id, second.id))
    assert await native.store.delete_user(native.setup, first.id, None) is None
    remaining: Final = await native.store.get_group(native.setup, current.id)
    assert isinstance(remaining, GroupRecord) and remaining.members == (second.id,) and remaining.version == current.version+1
    assert await native.store.delete_group(native.setup, current.id, None) is None
    assert await revision(native, second) != second.credential_revision
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        members: Final = await writer.query_raw("SELECT removed FROM retail_scim_group_members")
        counts: Final = await writer.query_raw("SELECT retained_membership_count,retained_group_count FROM retail_scim_settings")
    assert members == [{"removed": True}, {"removed": True}]
    assert counts == [{"retained_membership_count": 2, "retained_group_count": 1}]


@pytest.mark.asyncio
async def test_access_criteria_changes_rotate_every_managed_revision_without_reactivation_or_old_proof_revival(native: Fixture) -> None:
    active: Final = await created(native)
    inactive: Final = await created(native, "inactive", active=False)
    original: Final = await issued(native, active)
    restricted: Final = await configured(native, {"access_group_external_ids": ("group-eligible", "another")})
    revision_after_policy: Final = await revision(restricted, active)
    assert revision_after_policy != active.credential_revision
    assert await revision(restricted, inactive) != inactive.credential_revision
    with pytest.raises(psycopg.errors.RaiseException):
        await issued(restricted, active, "restricted")
    await group(restricted, "eligible", (active.id,))
    await issued(restricted, active, "allowed")
    reordered: Final = await configured(restricted, {"access_group_external_ids": ("another", "group-eligible")})
    assert await revision(reordered, active) == revision_after_policy
    loosened: Final = await configured(reordered, {"access_group_external_ids": ()})
    assert await revision(loosened, active) != revision_after_policy
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        proof: Final = await writer.query_raw("SELECT credential_revision FROM retail_scim_credentials WHERE token_hash=$1", original)
    assert proof == [{"credential_revision": active.credential_revision}]


@pytest.mark.asyncio
async def test_group_and_retained_membership_caps_include_tombstones_and_readding_existing_edge_does_not_allocate(native: Fixture) -> None:
    first: Final = await created(native)
    second: Final = await created(native, "two")
    bounded: Final = await configured(native, {"max_groups": 2, "max_memberships": 1})
    current: Final = await group(bounded, members=(first.id,))
    remove: Final = await native.store.update_group(bounded.setup, current.id, patch({"op": "remove", "path": "members"}), replace=False, if_match=None)
    assert isinstance(remove, GroupRecord)
    add_old: Final = await native.store.update_group(bounded.setup, current.id,
        patch({"op": "add", "path": "members", "value": [{"value": str(first.id)}]}), replace=False, if_match=None)
    assert isinstance(add_old, GroupRecord)
    assert await native.store.create_group(bounded.setup, profile("two", (second.id,))) == ScimFailure(503)
    empty: Final = await group(bounded, "two")
    assert await native.store.delete_group(bounded.setup, empty.id, None) is None
    assert await native.store.create_group(bounded.setup, profile("three")) == ScimFailure(503)


@pytest.mark.asyncio
async def test_group_filters_pages_and_soft_deleted_members_are_consistent(native: Fixture) -> None:
    first: Final = await group(native)
    second: Final = await group(native, "two")
    assert await native.store.list_groups(native.setup, GroupQuery(count=1, start_index=2)) == GroupList(resources=(second,), total=2, start_index=2)
    assert await native.store.list_groups(native.setup, GroupQuery(count=0)) == GroupList(resources=(), total=2, start_index=1)
    for field, value in (("id", str(first.id)), ("external_id", first.external_id), ("display_name", first.display_name)):
        assert await native.store.list_groups(native.setup, GroupQuery(filter_field=field, filter_value=value)) == GroupList(resources=(first,), total=1, start_index=1)
    assert await native.store.list_groups(native.setup, GroupQuery(filter_field="display_name", filter_value=first.display_name.upper())) == GroupList(resources=(), total=0, start_index=1)
    assert await native.store.list_groups(native.setup, GroupQuery(filter_field="external_id", filter_value="' OR true --")) == GroupList(resources=(), total=0, start_index=1)


@pytest.mark.asyncio
async def test_outbox_capacity_rolls_back_group_membership_revisions_and_policy_changes(native: Fixture) -> None:
    user: Final = await created(native)
    current: Final = await group(native, members=(user.id,))
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("UPDATE retail_native_audit_settings SET retained_count=10000 WHERE singleton")
    assert await native.store.update_group(native.setup, current.id, patch({"op": "remove", "path": "members"}), replace=False, if_match=None) == ScimFailure(503)
    assert await native.store.delete_group(native.setup, current.id, None) == ScimFailure(503)
    assert await native.store.create_group(native.setup, profile("two")) == ScimFailure(503)
    setup: Final = ScimSetup(native.setup.config.model_copy(update={"generation": 2, "access_group_external_ids": ("eligible",)}), native.setup.token)
    assert await native.store.configure(setup) == ScimFailure(503)
    assert await revision(native, user) == user.credential_revision
    assert await native.store.get_group(native.setup, current.id) == current


async def _waiting(writer: Writer) -> None:
    for _ in range(30):
        await writer.query_raw("SELECT pg_stat_clear_snapshot()")
        rows: Final = await writer.query_raw("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND position('SELECT id' in query)=1 AND position('retail_scim_users' in query)>0 AND position('FOR UPDATE' in query)>0) AS waiting")
        if rows == [{"waiting": True}]:
            return
        await asyncio.sleep(.005)
    raise AssertionError("Expected the directory writer to wait for the owned resource lock")


@pytest.mark.asyncio
async def test_policy_change_waits_for_issuance_resource_without_holding_settings_counter_lock(native: Fixture) -> None:
    user: Final = await created(native)
    setup: Final = ScimSetup(native.setup.config.model_copy(update={"generation": 2, "access_group_external_ids": ("eligible",)}), native.setup.token)
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw("SELECT id FROM retail_scim_users WHERE id=$1::uuid FOR UPDATE", str(user.id))
        change: Final = asyncio.create_task(native.store.configure(setup))
        try:
            await _waiting(writer)
            await writer.query_raw('INSERT INTO "LiteLLM_VerificationToken"(token,user_id,models) VALUES($1,$2,\'{}\') RETURNING token', hash_token("race-key"), user.user_id)
        except BaseException:
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)
            raise
    assert await change is None
    assert await revision(Fixture(native.client, native.store, setup), user) != user.credential_revision


@pytest.mark.asyncio
async def test_membership_removal_waits_for_issuance_then_revokes_the_just_issued_proof(native: Fixture) -> None:
    user: Final = await created(native)
    current: Final = await group(native, members=(user.id,))
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.query_raw("SELECT id FROM retail_scim_users WHERE id=$1::uuid FOR UPDATE", str(user.id))
        change: Final = asyncio.create_task(native.store.update_group(native.setup, current.id,
            patch({"op": "remove", "path": "members"}), replace=False, if_match=None))
        try:
            await _waiting(writer)
            await writer.query_raw('INSERT INTO "LiteLLM_VerificationToken"(token,user_id,models) VALUES($1,$2,\'{}\') RETURNING token', hash_token("race-key"), user.user_id)
        except BaseException:
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)
            raise
    assert isinstance(await change, GroupRecord)
    assert await revision(native, user) != user.credential_revision


@pytest.mark.asyncio
async def test_existing_r13_schema_upgrade_preserves_empty_policy_native_key_and_proof(native: Fixture) -> None:
    user: Final = await created(native)
    token: Final = await issued(native, user)
    legacy_config: Final = native.setup.config.model_dump(mode="json", exclude={"max_groups", "max_memberships", "access_group_external_ids"})
    legacy_digest: Final = hashlib.sha256(json.dumps({"config": legacy_config,
        "token_hash": hashlib.sha256(native.setup.token.get_secret_value().encode()).hexdigest()}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("DROP TABLE retail_scim_group_members,retail_scim_groups")
        await writer.execute_raw("ALTER TABLE retail_scim_settings DROP COLUMN max_groups,DROP COLUMN retained_group_count,DROP COLUMN max_memberships,DROP COLUMN retained_membership_count,DROP COLUMN access_group_external_ids")
        await writer.query_raw("UPDATE retail_scim_settings SET digest=$1 RETURNING singleton", legacy_digest)
    assert await native.store.initialize() is None
    upgraded: Final = await configured(native, {})
    assert await revision(upgraded, user) == user.credential_revision
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        rows: Final = await writer.query_raw("SELECT credential_revision FROM retail_scim_credentials WHERE token_hash=$1", token)
    assert rows == [{"credential_revision": user.credential_revision}]


class SeedIds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ids: tuple[UUID, ...]


@pytest.mark.asyncio
async def test_list_member_budget_returns_a_stable_prefix_and_next_page_without_skipping_empty_groups(native: Fixture) -> None:
    async with native.client.tx(timeout=timedelta(seconds=8)) as writer:
        await writer.execute_raw("CREATE TEMP TABLE budget_fixture AS SELECT gen_random_uuid() AS id,'retail-scim-user:'||gen_random_uuid()::text AS user_id FROM generate_series(1,600)")
        await writer.query_raw("SELECT set_config('retail.scim_provision','on',true)")
        await writer.execute_raw('INSERT INTO "LiteLLM_UserTable"(user_id,user_role,models) SELECT user_id,\'customer\',\'{}\' FROM budget_fixture')
        await writer.query_raw("SELECT set_config('retail.scim_provision','off',true)")
        await writer.query_raw("""INSERT INTO retail_scim_users(id,user_id,namespace,external_id,user_name,user_name_normalized,active,credential_revision)
            SELECT id,user_id,$1,id::text,id::text,id::text,true,gen_random_uuid() FROM budget_fixture RETURNING id""", native.setup.config.namespace)
        await writer.execute_raw("UPDATE retail_scim_settings SET retained_count=retained_count+600")
        rows: Final = TypeAdapter(tuple[SeedIds, ...]).validate_python(await writer.query_raw("SELECT array_agg(id ORDER BY id) AS ids FROM budget_fixture"))
    empty_first: Final = await group(native, "empty-first")
    full_first: Final = await group(native, "dense-first", rows[0].ids)
    full_second: Final = await group(native, "dense-second", rows[0].ids)
    empty_last: Final = await group(native, "empty-last")
    first_page: Final = await native.store.list_groups(native.setup, GroupQuery())
    assert first_page == GroupList(resources=(empty_first, full_first), total=4, start_index=1)
    second_page: Final = await native.store.list_groups(native.setup, GroupQuery(start_index=3))
    assert second_page == GroupList(resources=(full_second, empty_last), total=4, start_index=3)
    assert await native.store.list_groups(native.setup, GroupQuery(start_index=10000)) == GroupList(resources=(), total=4, start_index=10000)


@pytest.mark.asyncio
async def test_removed_membership_cannot_issue_a_new_key_even_though_the_edge_tombstone_remains(native: Fixture) -> None:
    user: Final = await created(native)
    restricted: Final = await configured(native, {"access_group_external_ids": ("group-eligible",)})
    current: Final = await group(restricted, "eligible", (user.id,))
    await issued(restricted, user)
    assert isinstance(await native.store.update_group(restricted.setup, current.id,
        patch({"op": "remove", "path": "members"}), replace=False, if_match=None), GroupRecord)
    with pytest.raises(psycopg.errors.RaiseException):
        await issued(restricted, user, "removed")


@dataclass(frozen=True, slots=True)
class SnapshotWriter:
    writer: Writer
    mutate: Callable[[], Awaitable[None]]

    async def query_raw(self, query: str, *args: object) -> object:
        result: Final = await self.writer.query_raw(query, *args)
        if query.startswith("SELECT count(*) AS total FROM retail_scim_groups"):
            await self.mutate()
        return result

    async def execute_raw(self, query: str, *args: object) -> int:
        return await self.writer.execute_raw(query, *args)


@dataclass(frozen=True, slots=True)
class SnapshotClient:
    native: Fixture
    mutate: Callable[[], Awaitable[None]]

    @asynccontextmanager
    async def tx(self, *, timeout: timedelta) -> AsyncIterator[SnapshotWriter]:
        async with self.native.client.tx(timeout=timeout) as writer:
            yield SnapshotWriter(writer, self.mutate)


@pytest.mark.asyncio
async def test_groups_count_and_page_share_one_snapshot_during_concurrent_create(native: Fixture) -> None:
    current: Final = await group(native)

    async def add_between_queries() -> None:
        await group(native, "concurrent")

    store: Final = NativeScimStore(SnapshotClient(native, add_between_queries))
    assert await store.list_groups(native.setup, GroupQuery()) == GroupList(resources=(current,), total=1, start_index=1)

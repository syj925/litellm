import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Final
from uuid import UUID

import pytest

from litellm.proxy.retail.scim_config import ScimConfig
from litellm.proxy.retail.scim_groups_protocol import (
    GROUP_SCHEMA,
    GroupChange,
    GroupList,
    GroupProfile,
    GroupQuery,
    GroupRecord,
    apply_group_patch,
    group_etag,
    group_resource_type,
    group_schema_document,
    matches_group_etag,
    parse_group_create,
    parse_group_query,
    parse_group_replace,
    parse_group_search,
    render_group,
    render_group_list,
)
from litellm.proxy.retail.scim_protocol import (
    LIST_SCHEMA,
    PATCH_SCHEMA,
    SEARCH_SCHEMA,
    ScimFailure,
    resource_types,
    schema_documents,
)

_FIRST: Final = UUID("11111111-1111-4111-8111-111111111111")
_SECOND: Final = UUID("22222222-2222-4222-8222-222222222222")
_THIRD: Final = UUID("33333333-3333-4333-8333-333333333333")


def body(changes: Mapping[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "schemas": [GROUP_SCHEMA],
            "externalId": "private-group",
            "displayName": "团队 Alpha",
            "members": [{"value": str(_FIRST)}, {"value": str(_SECOND)}],
            **(changes or {}),
        }
    ).encode()


def current() -> GroupRecord:
    return GroupRecord(
        id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        namespace="private-namespace",
        external_id="private-group",
        display_name="团队 Alpha",
        members=(_FIRST, _SECOND),
        version=3,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def patch(operations: Sequence[Mapping[str, object]]) -> bytes:
    return json.dumps({"schemas": [PATCH_SCHEMA], "Operations": operations}).encode()


def test_create_keeps_only_scim_user_resource_ids_and_ignores_readonly_client_member_metadata() -> None:
    result: Final = parse_group_create(
        body(
            {
                "id": "existing-native-id",
                "meta": {"role": "admin"},
                "members": [
                    {
                        "VALUE": str(_SECOND),
                        "$ref": "https://foreign.test/Groups/admin",
                        "display": "private",
                        "type": "Group",
                    },
                    {"value": str(_FIRST)},
                ],
            }
        )
    )
    assert result == GroupProfile(external_id="private-group", display_name="团队 Alpha", members=(_FIRST, _SECOND))
    assert "private" not in repr(result) and "Alpha" not in repr(result)
    assert parse_group_create(
        b'{"schemas":["' + GROUP_SCHEMA.encode() + b'"],"externalId":"x","displayName":"Empty"}'
    ) == GroupProfile(external_id="x", display_name="Empty")


@pytest.mark.parametrize(
    "changes",
    (
        {"schemas": []},
        {"schemas": [GROUP_SCHEMA, GROUP_SCHEMA]},
        {"schemas": ["urn:foreign"]},
        {"externalId": ""},
        {"externalId": "x" * 256},
        {"externalId": "身份"},
        {"externalId": "x\ny"},
        {"displayName": ""},
        {"displayName": None},
        {"displayName": "x" * 256},
        {"displayName": "x\x00y"},
        {"displayName": "x\u202ey"},
        {"displayName": 123},
        {"active": True},
        {"roles": ["admin"]},
        {"password": "private"},
        {"native_user_id": "native"},
        {"members": None},
        {"members": {}},
        {"members": [str(_FIRST)]},
        {"members": [{"value": "retail-scim-user:" + str(_FIRST)}]},
        {"members": [{"value": 123}]},
        {"members": [{"value": str(_FIRST), "user_id": "native"}]},
        {"members": [{"value": str(_FIRST)}, {"value": str(_FIRST).upper()}]},
    ),
)
def test_create_rejects_unsupported_authority_fields_invalid_profile_and_duplicate_member_ids(
    changes: Mapping[str, object],
) -> None:
    result: Final = parse_group_create(body(changes))
    assert isinstance(result, ScimFailure) and result.status == 400


@pytest.mark.parametrize("raw", (b"[]", b"null", b"\xff", b"NaN", b"{", b'"text"', b" " * 65537))
def test_group_json_boundary_rejects_invalid_or_oversized_input(raw: bytes) -> None:
    assert isinstance(parse_group_create(raw), ScimFailure)
    assert isinstance(apply_group_patch(raw, current()), ScimFailure)
    assert isinstance(parse_group_search(raw), ScimFailure)


def test_duplicate_attributes_cannot_override_an_otherwise_valid_group_profile() -> None:
    raw: Final = body().decode()[:-1] + ',"DISPLAYNAME":"override"}'
    assert parse_group_create(raw.encode()) == ScimFailure(400, "invalidSyntax")


def test_member_cap_applies_to_create_and_accumulated_patch_memberships() -> None:
    members: Final = tuple(UUID(int=i) for i in range(1, 1001))
    valid: Final = [{"value": str(member)} for member in members]
    assert isinstance(parse_group_create(body({"members": valid})), GroupProfile)
    assert parse_group_create(body({"members": [*valid, {"value": str(UUID(int=1001))}]})) == ScimFailure(
        400, "invalidValue"
    )
    full: Final = current().model_copy(update={"members": members})
    overflow: Final = apply_group_patch(
        patch(({"op": "add", "path": "members", "value": [{"value": str(UUID(int=1001))}]},)), full
    )
    assert overflow == ScimFailure(400, "invalidValue") and len(full.members) == 1000


@pytest.mark.parametrize(
    "expression,field,value",
    (
        ('id eq "unknown-id"', "id", "unknown-id"),
        ('externalId EQ "Case"', "external_id", "Case"),
        ('DISPLAYNAME eq "团队 Alpha"', "display_name", "团队 Alpha"),
        ('displayName eq "MiXeD"', "display_name", "MiXeD"),
        ('externalId eq "x\' OR true --"', "external_id", "x' OR true --"),
    ),
)
def test_group_filter_returns_typed_parameter_values_and_preserves_exact_display_name(
    expression: str,
    field: str,
    value: str,
) -> None:
    query: Final = parse_group_query((("filter", expression),))
    assert isinstance(query, GroupQuery) and query.filter_field == field and query.filter_value == value


@pytest.mark.parametrize(
    "expression",
    (
        'userName eq "x"',
        'members.value eq "x"',
        'displayName co "x"',
        'displayName eq "x" or externalId eq "y"',
        "active eq true",
        "displayName eq true",
        "displayName eq null",
        'displayName eq "bad\\nname"',
        'displayName eq "\ud800"',
        'İD eq "x"',
        'externalId eq "身份"',
        "",
    ),
)
def test_unknown_operators_paths_and_invalid_filter_types_are_rejected_without_exceptions(expression: str) -> None:
    assert parse_group_query((("filter", expression),)) == ScimFailure(400, "invalidFilter")


def test_group_query_bounds_and_search_have_the_same_defined_profile() -> None:
    query: Final = parse_group_query((("startIndex", "999999999999999999999999999999"), ("count", "1000")))
    assert query == GroupQuery(start_index=10000, count=100)
    assert parse_group_query((("startIndex", "-2"), ("count", "-1"))) == GroupQuery(start_index=1, count=0)
    payload: Final = {"schemas": [SEARCH_SCHEMA], "startIndex": 2, "count": 3, "filter": 'displayName eq "团队"'}
    assert parse_group_search(json.dumps(payload).encode()) == parse_group_query(
        (("startIndex", "2"), ("count", "3"), ("filter", 'displayName eq "团队"'))
    )
    for invalid in (True, 1.5, "2", None):
        assert isinstance(parse_group_search(json.dumps({**payload, "count": invalid}).encode()), ScimFailure)


@pytest.mark.parametrize(
    "query",
    (
        (("count", "1"), ("COUNT", "2")),
        (("sortBy", "displayName"),),
        (("cursor", "x"),),
        (("count", "1.5"),),
        (("startIndex", "false"),),
    ),
)
def test_group_query_rejects_unknown_duplicate_and_non_integer_arguments(query: Sequence[tuple[str, str]]) -> None:
    assert isinstance(parse_group_query(query), ScimFailure)


def test_members_add_replace_and_valuepath_remove_preserve_all_intermediate_removals() -> None:
    result: Final = apply_group_patch(
        patch(
            (
                {"op": "remove", "path": 'members[value eq "' + str(_FIRST) + '"]'},
                {"op": "add", "path": "members", "value": [{"value": str(_FIRST)}, {"value": str(_THIRD)}]},
                {
                    "op": "replace",
                    "value": {"members": [{"value": str(_FIRST)}, {"value": str(_SECOND)}], "displayName": "Changed"},
                },
            )
        ),
        current(),
    )
    assert isinstance(result, GroupChange)
    assert result.profile.members == (_FIRST, _SECOND) and result.profile.display_name == "Changed"
    assert set(result.removed_members) == {_FIRST, _THIRD}


def test_member_reorder_duplicate_existing_add_and_missing_remove_are_noops() -> None:
    reordered: Final = apply_group_patch(
        patch(({"op": "replace", "path": "members", "value": [{"value": str(_SECOND)}, {"value": str(_FIRST)}]},)),
        current(),
    )
    duplicate_add: Final = apply_group_patch(
        patch(({"op": "add", "path": "members", "value": [{"value": str(_FIRST)}]},)), current()
    )
    absent_remove: Final = apply_group_patch(
        patch(({"op": "remove", "path": 'members[value eq "' + str(_THIRD) + '"]'},)), current()
    )
    for result in (reordered, duplicate_add, absent_remove):
        assert (
            isinstance(result, GroupChange)
            and result.profile.members == current().members
            and result.removed_members == ()
        )


def test_remove_all_members_records_all_of_them_and_empty_removal_is_idempotent() -> None:
    result: Final = apply_group_patch(patch(({"op": "remove", "path": "members"},)), current())
    assert (
        isinstance(result, GroupChange) and result.profile.members == () and result.removed_members == current().members
    )
    empty: Final = current().model_copy(update={"members": ()})
    again: Final = apply_group_patch(patch(({"op": "remove", "path": "members"},)), empty)
    assert isinstance(again, GroupChange) and again.removed_members == ()


@pytest.mark.parametrize(
    "operation,error",
    (
        ({"op": "remove"}, "noTarget"),
        ({"op": "remove", "path": "displayName"}, "mutability"),
        ({"op": "replace", "path": "externalId", "value": "other"}, "mutability"),
        ({"op": "replace", "path": "meta.version", "value": "other"}, "mutability"),
        ({"op": "replace", "value": {"id": "other"}}, "mutability"),
        ({"op": "remove", "path": "meta"}, "mutability"),
        ({"op": "add", "path": "roles", "value": ["admin"]}, "invalidPath"),
        ({"op": "remove", "path": 'members[value eq "not-a-uuid"]'}, "invalidPath"),
        ({"op": "remove", "path": 'members[display eq "x"]'}, "invalidPath"),
    ),
)
def test_invalid_later_patch_operation_returns_no_partial_membership_plan(
    operation: Mapping[str, object], error: str
) -> None:
    original: Final = current()
    plan: Final = apply_group_patch(patch(({"op": "remove", "path": "members"}, operation)), original)
    assert plan == ScimFailure(400, error) and original.members == (_FIRST, _SECOND)


def test_patch_limit_and_immutable_external_id_equal_value_noop() -> None:
    operation: Final = {"op": "replace", "path": "externalId", "value": current().external_id}
    assert isinstance(apply_group_patch(patch((operation,) * 16), current()), GroupChange)
    assert isinstance(apply_group_patch(patch((operation,) * 17), current()), ScimFailure)
    assert isinstance(apply_group_patch(patch(()), current()), ScimFailure)


def test_put_omission_clears_members_and_get_roundtrip_ignores_readonly_fields() -> None:
    config: Final = ScimConfig(namespace="directory", generation=1, public_origin="https://trusted.test")
    rendered: Final = render_group(current(), config)
    roundtrip: Final = parse_group_replace(json.dumps(rendered).encode(), current())
    assert (
        isinstance(roundtrip, GroupChange)
        and roundtrip.profile.members == current().members
        and not roundtrip.removed_members
    )
    without_members: Final = parse_group_replace(
        json.dumps(
            {
                "schemas": [GROUP_SCHEMA],
                "externalId": current().external_id,
                "displayName": "Changed",
                "id": "ignored",
                "meta": {"version": "ignored"},
            }
        ).encode(),
        current(),
    )
    assert (
        isinstance(without_members, GroupChange)
        and without_members.profile.members == ()
        and without_members.removed_members == current().members
    )
    assert parse_group_replace(body({"externalId": "other"}), current()) == ScimFailure(400, "mutability")


def test_etag_is_optional_exact_and_malformed_headers_fail_closed() -> None:
    original: Final = current()
    assert matches_group_etag(None, original) is True and matches_group_etag("*", original) is True
    assert matches_group_etag(group_etag(original), original) is True
    assert matches_group_etag(group_etag(original.model_copy(update={"version": 2})), original) is False
    assert matches_group_etag('W/"other"', original) is False
    assert matches_group_etag('"strong"', original) == ScimFailure(400, "invalidSyntax")


def test_rendering_exposes_only_group_identity_user_refs_and_supported_discovery() -> None:
    config: Final = ScimConfig(namespace="directory", generation=1, public_origin="https://trusted.test")
    rendered: Final = render_group(current(), config)
    assert isinstance(rendered, dict) and rendered["displayName"] == current().display_name
    assert rendered["members"] == [
        {"value": str(member), "$ref": f"https://trusted.test/retail/scim/v2/Users/{member}", "type": "User"}
        for member in current().members
    ]
    assert current().namespace not in json.dumps(rendered) and "user_id" not in json.dumps(rendered)
    assert render_group_list(GroupList(resources=(current(),), total=4, start_index=2), config) == {
        "schemas": [LIST_SCHEMA],
        "totalResults": 4,
        "startIndex": 2,
        "itemsPerPage": 1,
        "Resources": [rendered],
    }
    resources: Final = resource_types(config)
    schemas: Final = schema_documents(config)
    assert isinstance(resources, dict) and group_resource_type(config) in resources["Resources"]
    assert isinstance(schemas, dict) and group_schema_document(config) in schemas["Resources"]

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Final
from uuid import UUID

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.scim_config import ScimConfig
from litellm.proxy.retail.scim_protocol import (
    ERROR_SCHEMA,
    LIST_SCHEMA,
    PATCH_SCHEMA,
    SEARCH_SCHEMA,
    USER_SCHEMA,
    ScimChange,
    ScimFailure,
    ScimList,
    ScimProfile,
    ScimQuery,
    ScimRecord,
    apply_patch,
    etag,
    matches_etag,
    parse_create,
    parse_query,
    parse_replace,
    parse_search,
    render_error,
    render_list,
    render_user,
    resource_types,
    schema_documents,
    service_provider_config,
)


def body(changes: Mapping[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "schemas": [USER_SCHEMA],
            "externalId": "external-private",
            "userName": "Directory.User",
            "displayName": "名字",
            "active": True,
            **(changes or {}),
        }
    ).encode()


def record() -> ScimRecord:
    return ScimRecord(
        id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        user_id="retail-scim-user:private-native",
        namespace="private-namespace",
        external_id="external-private",
        user_name="Directory.User",
        display_name="名字",
        active=True,
        version=3,
        credential_revision=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def patch(operations: Sequence[Mapping[str, object]]) -> bytes:
    return json.dumps({"schemas": [PATCH_SCHEMA], "Operations": operations}).encode()


def test_create_accepts_case_insensitive_attributes_without_turning_profile_into_native_authority() -> None:
    value: Final = parse_create(body())
    assert value == ScimProfile(
        external_id="external-private", user_name="Directory.User", display_name="名字", active=True
    )
    assert parse_create(
        b'{"SCHEMAS":["' + USER_SCHEMA.encode() + b'"],"EXTERNALID":"opaque","USERNAME":"User"}'
    ) == ScimProfile(external_id="opaque", user_name="User", active=True)
    assert "external-private" not in repr(value) and "Directory.User" not in repr(value)


@pytest.mark.parametrize(
    "changes",
    (
        {"schemas": []},
        {"schemas": [USER_SCHEMA, USER_SCHEMA]},
        {"schemas": [PATCH_SCHEMA]},
        {"externalId": ""},
        {"externalId": "x" * 256},
        {"externalId": "身份"},
        {"externalId": "a\nb"},
        {"userName": ""},
        {"userName": "has space"},
        {"userName": "名字"},
        {"userName": "x" * 256},
        {"active": 1},
        {"active": "false"},
        {"active": None},
        {"displayName": "bad\x00text"},
        {"displayName": "bad\u202etext"},
        {"displayName": "x" * 256},
        {"displayName": 123},
        {"password": "fixture-private-password"},
        {"roles": ["proxy_admin"]},
        {"groups": ["admin"]},
        {"user_id": "existing-admin"},
        {"emails": [{"value": "existing@example.invalid"}]},
    ),
)
def test_create_rejects_unsupported_or_invalid_directory_fields(changes: Mapping[str, object]) -> None:
    failure: Final = parse_create(body(changes))
    assert isinstance(failure, ScimFailure) and failure.status == 400
    assert "private-password" not in str(render_error(failure)) and "existing-admin" not in str(render_error(failure))


@pytest.mark.parametrize(
    "raw",
    (
        b'{"active":true,"active":false}',
        b'{"active":true,"ACTIVE":false}',
        b'{"nested":{"x":1,"x":2}}',
        b"NaN",
        b"[]",
        b"null",
        b'"text"',
        b"\xff",
        b"{",
    ),
)
def test_invalid_json_and_duplicate_attributes_fail_closed_without_echo(raw: bytes) -> None:
    assert isinstance(parse_create(raw), ScimFailure)
    assert isinstance(apply_patch(raw, record()), ScimFailure)
    assert isinstance(parse_search(raw), ScimFailure)


@pytest.mark.parametrize("duplicate", ('"active":false', '"ACTIVE":false'))
def test_duplicate_attribute_cannot_override_an_otherwise_valid_profile(duplicate: str) -> None:
    raw: Final = body().decode()[:-1] + "," + duplicate + "}"
    assert parse_create(raw.encode()) == ScimFailure(400, "invalidSyntax")


@pytest.mark.parametrize(
    "expression,field,value",
    (
        ('userName eq "DIRECTORY.User"', "user_name", "directory.user"),
        ('EXTERNALID EQ "Case-Sensitive"', "external_id", "Case-Sensitive"),
        ('id eq "not-a-uuid"', "id", "not-a-uuid"),
        ("active eq true", "active", True),
        ("active EQ false", "active", False),
        ('externalId eq "a\\"b"', "external_id", 'a"b'),
        ('externalId eq "x\' OR true --"', "external_id", "x' OR true --"),
    ),
)
def test_filter_only_produces_typed_values_and_normalizes_case_insensitive_username(
    expression: str,
    field: str,
    value: str | bool,
) -> None:
    result: Final = parse_query((("filter", expression),))
    assert isinstance(result, ScimQuery) and result.filter_field == field and result.filter_value == value


@pytest.mark.parametrize(
    "expression",
    (
        "",
        'userName co "a"',
        'email eq "a"',
        'userName eq "a" or active eq true',
        "userName eq null",
        "userName eq 1",
        'active eq "true"',
        "active eq 1",
        "active eq TRUE",
        'externalId eq "x" trailing',
        'externalId eq "a\\n"',
        'externalId eq "' + "x" * 256 + '"',
        "userName[active eq true]",
        "not (active eq true)",
        'userName eq "unclosed',
        'userName eq "\ud800"',
    ),
)
def test_unsupported_filter_syntax_or_types_returns_standard_invalid_filter(expression: str) -> None:
    assert parse_query((("filter", expression),)) == ScimFailure(400, "invalidFilter")


@pytest.mark.parametrize("attribute", ("İD", "uſerName"))
def test_unicode_case_folding_cannot_turn_unknown_attributes_into_server_exceptions(attribute: str) -> None:
    assert parse_query((("filter", attribute + ' eq "x"'),)) == ScimFailure(400, "invalidFilter")


@pytest.mark.parametrize(
    "start,count,expected_start,expected_count",
    (("-4", "-3", 1, 0), ("0", "0", 1, 0), ("2", "999", 2, 100), ("100", "7", 100, 7)),
)
def test_index_pagination_clamps_without_wrapping_or_exceeding_the_page_bound(
    start: str,
    count: str,
    expected_start: int,
    expected_count: int,
) -> None:
    result: Final = parse_query((("startIndex", start), ("count", count)))
    assert result == ScimQuery(start_index=expected_start, count=expected_count)


@pytest.mark.parametrize(
    "params",
    (
        (("count", "1"), ("COUNT", "2")),
        (("sortBy", "userName"),),
        (("attributes", "roles"),),
        (("startIndex", "1.5"),),
        (("count", "true"),),
        (("cursor", "token"),),
    ),
)
def test_duplicate_unknown_or_non_integer_query_arguments_are_rejected(params: Sequence[tuple[str, str]]) -> None:
    assert isinstance(parse_query(params), ScimFailure)


def test_search_body_matches_index_query_but_does_not_coerce_boolean_or_float_counts() -> None:
    search: Final = {"schemas": [SEARCH_SCHEMA], "filter": 'userName eq "USER"', "startIndex": 2, "count": 3}
    assert parse_search(json.dumps(search).encode()) == parse_query(
        (("filter", 'userName eq "USER"'), ("startIndex", "2"), ("count", "3"))
    )
    for invalid in (True, 1.5, "2", None):
        assert isinstance(parse_search(json.dumps({**search, "count": invalid}).encode()), ScimFailure)


@pytest.mark.parametrize(
    "states,rotate", (((False,), True), ((False, True), True), ((True,), False), ((True, True), False))
)
def test_every_ordered_active_transition_rotates_credentials_even_if_final_value_is_unchanged(
    states: tuple[bool, ...],
    rotate: bool,
) -> None:
    result: Final = apply_patch(
        patch(tuple({"op": "replace", "path": "active", "value": state} for state in states)), record()
    )
    assert (
        isinstance(result, ScimChange) and result.profile.active == states[-1] and result.rotate_credentials == rotate
    )


def test_pathless_patch_and_display_removal_are_sequential_and_do_not_mutate_the_original_record() -> None:
    original: Final = record()
    result: Final = apply_patch(
        patch(
            (
                {"op": "ADD", "value": {"UserName": "New.User", "displayName": "Temporary"}},
                {"op": "remove", "path": "displayName"},
            )
        ),
        original,
    )
    assert isinstance(result, ScimChange)
    assert (
        result.profile.user_name == "New.User" and result.profile.display_name is None and not result.rotate_credentials
    )
    assert original.user_name == "Directory.User" and original.display_name == "名字"
    with pytest.raises(ValidationError):
        original.active = False


@pytest.mark.parametrize(
    "operation",
    (
        {"op": "replace", "path": "externalId", "value": "different"},
        {"op": "remove", "path": "externalId"},
        {"op": "replace", "path": "roles", "value": ["proxy_admin"]},
        {"op": "remove", "path": "active"},
        {"op": "replace", "path": "active", "value": "false"},
        {"op": "replace", "value": {"password": "private"}},
        {"op": "replace", "path": 'emails[value eq "x"]', "value": []},
        {"op": "copy", "path": "displayName", "value": "x"},
        {"op": "replace", "path": "displayName"},
        {"op": "remove", "path": "displayName", "value": "x"},
    ),
)
def test_invalid_later_patch_operation_returns_no_partial_plan(operation: Mapping[str, object]) -> None:
    original: Final = record()
    result: Final = apply_patch(patch(({"op": "replace", "path": "active", "value": False}, operation)), original)
    assert isinstance(result, ScimFailure) and result.status == 400
    assert original.active and original.version == 3


def test_patch_bound_and_same_external_identifier_noop() -> None:
    noop: Final = {"op": "replace", "path": "externalId", "value": record().external_id}
    assert isinstance(apply_patch(patch((noop,) * 16), record()), ScimChange)
    assert isinstance(apply_patch(patch((noop,) * 17), record()), ScimFailure)
    assert isinstance(apply_patch(patch(()), record()), ScimFailure)


def test_remove_without_a_target_returns_standard_no_target_and_discards_prior_changes() -> None:
    operations: Final = ({"op": "replace", "path": "active", "value": False}, {"op": "remove"})
    assert apply_patch(patch(operations), record()) == ScimFailure(400, "noTarget")


def test_oversized_input_is_rejected_before_json_parsing() -> None:
    oversized: Final = b" " * 65537
    assert parse_create(oversized) == ScimFailure(413)
    assert apply_patch(oversized, record()) == ScimFailure(413)
    assert parse_search(oversized) == ScimFailure(413)


def test_put_requires_explicit_active_preserves_external_identity_and_clears_omitted_display_name() -> None:
    result: Final = parse_replace(
        json.dumps(
            {"schemas": [USER_SCHEMA], "externalId": record().external_id, "userName": "Replacement", "active": False}
        ).encode(),
        record(),
    )
    assert isinstance(result, ScimChange) and result.rotate_credentials and result.profile.display_name is None
    assert isinstance(
        parse_replace(
            b'{"schemas":["' + USER_SCHEMA.encode() + b'"],"externalId":"external-private","userName":"x"}', record()
        ),
        ScimFailure,
    )
    assert parse_replace(body({"externalId": "different"}), record()) == ScimFailure(400, "mutability")


def test_get_representation_can_be_put_back_without_rebinding_readonly_identity_or_metadata() -> None:
    current: Final = record()
    config: Final = ScimConfig(namespace="directory", generation=1, public_origin="https://gateway.test")
    public: Final = render_user(current, config)
    assert isinstance(public, dict)
    replacement: Final = {
        **public,
        "id": "different-server-id",
        "meta": {"version": "untrusted", "location": "https://other.test"},
    }
    result: Final = parse_replace(json.dumps(replacement).encode(), current)
    assert isinstance(result, ScimChange) and not result.rotate_credentials
    assert result.profile == parse_create(body())
    assert current.id == record().id and current.version == 3


def test_create_ignores_readonly_common_fields_without_accepting_privilege_attributes() -> None:
    readonly: Final = {"id": "existing-admin", "meta": {"native_user_id": "existing-admin", "user_role": "proxy_admin"}}
    assert parse_create(body(readonly)) == parse_create(body())
    assert isinstance(parse_create(body({**readonly, "roles": ["proxy_admin"]})), ScimFailure)
    assert isinstance(parse_create(body({**readonly, "password": "private"})), ScimFailure)


def test_patch_readonly_identity_and_metadata_return_mutability_without_partial_changes() -> None:
    current: Final = record()
    for operation in (
        {"op": "replace", "path": "id", "value": "other"},
        {"op": "replace", "path": "meta.version", "value": "other"},
        {"op": "add", "value": {"id": "other", "meta": {"version": "other"}}},
        {"op": "remove", "path": "meta"},
    ):
        operations: Final = ({"op": "replace", "path": "active", "value": False}, operation)
        assert apply_patch(patch(operations), current) == ScimFailure(400, "mutability")
    assert current.active and current.version == 3


def test_optional_etag_compares_exact_version_and_identity_including_valid_stale_values() -> None:
    current: Final = record()
    assert matches_etag(None, current) is True and matches_etag("*", current) is True
    assert matches_etag(etag(current), current) is True
    assert matches_etag(etag(current.model_copy(update={"version": 2})), current) is False
    assert matches_etag('W/"other-resource-3"', current) is False


@pytest.mark.parametrize("header", ('"strong"', 'W/""', 'W/"bad\nvalue"', 'W/"x", W/"y"', "invalid", 'w/"x"'))
def test_malformed_or_unsupported_if_match_never_silently_becomes_unconditional(header: str) -> None:
    assert matches_etag(header, record()) == ScimFailure(400, "invalidSyntax")


def test_public_projection_has_standard_metadata_but_no_native_authority_or_credential_proof() -> None:
    config: Final = ScimConfig(namespace="directory", generation=1, public_origin="https://trusted-gateway.test")
    current: Final = record()
    rendered: Final = render_user(current, config)
    assert (
        isinstance(rendered, dict) and rendered["id"] == str(current.id) and rendered["userName"] == current.user_name
    )
    assert isinstance(rendered["meta"], dict) and rendered["meta"]["version"] == etag(current)
    assert rendered["meta"]["location"] == "https://trusted-gateway.test/retail/scim/v2/Users/" + str(current.id)
    public: Final = json.dumps(rendered)
    assert (
        current.user_id not in public
        and current.namespace not in public
        and str(current.credential_revision) not in public
    )
    assert render_list(ScimList(resources=(current,), total=7, start_index=2), config) == {
        "schemas": [LIST_SCHEMA],
        "totalResults": 7,
        "startIndex": 2,
        "itemsPerPage": 1,
        "Resources": [rendered],
    }
    assert render_error(ScimFailure(400, "invalidFilter")) == {
        "schemas": [ERROR_SCHEMA],
        "status": "400",
        "detail": "SCIM request is invalid",
        "scimType": "invalidFilter",
    }


def test_discovery_only_advertises_implemented_users_index_filter_patch_and_etag_features() -> None:
    config: Final = ScimConfig(namespace="directory", generation=1, public_origin="https://gateway.test")
    advertised: Final = service_provider_config(config)
    assert isinstance(advertised, dict) and advertised["bulk"] == {
        "supported": False,
        "maxOperations": 0,
        "maxPayloadSize": 0,
    }
    assert advertised["patch"] == advertised["etag"] == {"supported": True}
    assert advertised["filter"] == {"supported": True, "maxResults": 100}
    resources: Final = resource_types(config)
    schemas: Final = schema_documents(config)
    assert isinstance(resources, dict) and resources["totalResults"] == 1 and "Group" not in json.dumps(resources)
    assert isinstance(schemas, dict) and schemas["totalResults"] == 1 and "password" not in json.dumps(schemas)

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StrictInt, ValidationError, field_validator

from .scim_protocol import (
    LIST_SCHEMA,
    PATCH_SCHEMA,
    SCIM_BASE,
    SCIM_WEAK_ETAG,
    SEARCH_SCHEMA,
    ScimFailure,
    decode_scim_json,
    scim_object_fields,
    scim_schema_matches,
)

if TYPE_CHECKING:
    from .scim_config import ScimConfig

GROUP_SCHEMA: Final = "urn:ietf:params:scim:schemas:core:2.0:Group"
_FILTER: Final = re.compile(r"\s*(id|externalId|displayName)\s+eq\s+(.+?)\s*\Z", re.IGNORECASE | re.ASCII)
_MEMBER_PATH: Final = re.compile(r'members\[\s*value\s+eq\s+("(?:[^"\\]|\\.)*")\s*\]\Z', re.IGNORECASE | re.ASCII)
_FIELDS: Final = frozenset({"schemas", "externalid", "displayname", "members", "id", "meta"})


class GroupProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    external_id: str = Field(min_length=1, max_length=255, repr=False)
    display_name: str = Field(min_length=1, max_length=255, repr=False)
    members: tuple[UUID, ...] = Field(default=(), max_length=1000, repr=False)

    @field_validator("external_id")
    @classmethod
    def opaque_external_id(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise ValueError("Invalid directory group identifier")
        return value

    @field_validator("display_name")
    @classmethod
    def printable_display(cls, value: str) -> str:
        if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
            raise ValueError("Invalid directory group display name")
        return value

    @field_validator("members")
    @classmethod
    def unique_members(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(frozenset(value)) != len(value):
            raise ValueError("Duplicate directory group member")
        return tuple(sorted(value, key=str))


class GroupRecord(GroupProfile):
    id: UUID
    namespace: str = Field(repr=False)
    version: StrictInt = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class GroupList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    resources: tuple[GroupRecord, ...]
    total: StrictInt = Field(ge=0)
    start_index: StrictInt = Field(ge=1)


class GroupQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    start_index: StrictInt = Field(default=1, ge=1, le=10000)
    count: StrictInt = Field(default=100, ge=0, le=100)
    filter_field: Literal["id", "external_id", "display_name"] | None = None
    filter_value: str | None = Field(default=None, repr=False)


class GroupChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    profile: GroupProfile = Field(repr=False)
    removed_members: tuple[UUID, ...] = Field(default=(), repr=False)


def _members(value: object) -> tuple[UUID, ...] | ScimFailure:
    if not isinstance(value, list) or len(value) > 1000:
        return ScimFailure(400, "invalidValue")
    members: tuple[UUID, ...] = ()  # rebind-ok: collect validated member identities without retaining client metadata
    for item in value:
        fields: Final = scim_object_fields(item)
        if isinstance(fields, ScimFailure):
            return ScimFailure(400, "invalidValue")
        member_value: Final = fields.get("value")
        if (
            set(fields) - {"value", "display", "$ref", "type"}
            or not isinstance(member_value, str)
            or len(member_value) > 64
        ):
            return ScimFailure(400, "invalidValue")
        try:
            identifier: Final = UUID(member_value)
        except ValueError:
            return ScimFailure(400, "invalidValue")
        if identifier in members:
            return ScimFailure(400, "invalidValue")
        members = (*members, identifier)
    return members


def _profile(fields: Mapping[str, object]) -> GroupProfile | ScimFailure:
    if set(fields) - _FIELDS or not scim_schema_matches(fields.get("schemas"), GROUP_SCHEMA):
        return ScimFailure(400, "invalidValue")
    members: Final = _members(fields.get("members", []))
    if isinstance(members, ScimFailure):
        return members
    try:
        return GroupProfile.model_validate(
            {"external_id": fields.get("externalid"), "display_name": fields.get("displayname"), "members": members}
        )
    except ValidationError:
        return ScimFailure(400, "invalidValue")


def parse_group_create(body: bytes) -> GroupProfile | ScimFailure:
    value: Final = decode_scim_json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = scim_object_fields(value)
    return fields if isinstance(fields, ScimFailure) else _profile(fields)


def _filter(value: str) -> GroupQuery | ScimFailure:
    matched: Final = _FILTER.fullmatch(value) if len(value) <= 2048 else None
    if matched is None:
        return ScimFailure(400, "invalidFilter")
    try:
        parsed: Final = json.loads(matched[2])
    except (ValueError, RecursionError):
        return ScimFailure(400, "invalidFilter")
    field: Final = {"id": "id", "externalid": "external_id", "displayname": "display_name"}[matched[1].lower()]
    if not isinstance(parsed, str) or not 1 <= len(parsed) <= 255:
        return ScimFailure(400, "invalidFilter")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in parsed):
        return ScimFailure(400, "invalidFilter")
    if field != "display_name" and any(ord(character) > 126 for character in parsed):
        return ScimFailure(400, "invalidFilter")
    return GroupQuery.model_validate({"filter_field": field, "filter_value": parsed})


def _query(fields: Mapping[str, object]) -> GroupQuery | ScimFailure:
    if set(fields) - {"startindex", "count", "filter"}:
        return ScimFailure(400, "invalidValue")
    try:
        start: Final = int(str(fields.get("startindex", 1)))
        count: Final = int(str(fields.get("count", 100)))
    except ValueError:
        return ScimFailure(400, "invalidValue")
    expression: Final = fields.get("filter")
    if expression is not None and not isinstance(expression, str):
        return ScimFailure(400, "invalidFilter")
    filtered: Final = _filter(expression) if isinstance(expression, str) else GroupQuery()
    if isinstance(filtered, ScimFailure):
        return filtered
    return filtered.model_copy(update={"start_index": min(10000, max(1, start)), "count": min(100, max(0, count))})


def parse_group_query(params: Sequence[tuple[str, str]]) -> GroupQuery | ScimFailure:
    if len(params) > 3 or len(frozenset(name.lower() for name, _ in params)) != len(params):
        return ScimFailure(400, "invalidValue")
    return _query({name.lower(): value for name, value in params})


def parse_group_search(body: bytes) -> GroupQuery | ScimFailure:
    value: Final = decode_scim_json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = scim_object_fields(value)
    if isinstance(fields, ScimFailure):
        return fields
    if not scim_schema_matches(fields.get("schemas"), SEARCH_SCHEMA):
        return ScimFailure(400, "invalidValue")
    if any(
        name in fields and (isinstance(fields[name], bool) or not isinstance(fields[name], int))
        for name in ("startindex", "count")
    ):
        return ScimFailure(400, "invalidValue")
    return _query({name: item for name, item in fields.items() if name != "schemas"})


def _current(current: GroupRecord) -> GroupProfile:
    return GroupProfile(external_id=current.external_id, display_name=current.display_name, members=current.members)


def _readonly(path: str) -> bool:
    return path in {"id", "meta"} or path.startswith("meta.") or path.endswith((".display", ".$ref", ".type"))


def _change(profile: GroupProfile, changes: Mapping[str, object], *, add: bool) -> GroupProfile | ScimFailure:
    if any(_readonly(name) for name in changes):
        return ScimFailure(400, "mutability")
    if set(changes) - {"externalid", "displayname", "members"}:
        return ScimFailure(400, "invalidPath")
    if "externalid" in changes and changes["externalid"] != profile.external_id:
        return ScimFailure(400, "mutability")
    members: Final = _members(changes["members"]) if "members" in changes else ()
    if isinstance(members, ScimFailure):
        return members
    merged: Final = (
        (*profile.members, *(identifier for identifier in members if identifier not in profile.members))
        if add
        else members
    )
    if len(merged) > 1000:
        return ScimFailure(400, "invalidValue")
    try:
        return GroupProfile.model_validate(
            {
                "external_id": profile.external_id,
                "display_name": changes.get("displayname", profile.display_name),
                "members": merged if "members" in changes else profile.members,
            }
        )
    except ValidationError:
        return ScimFailure(400, "invalidValue")


def _remove(profile: GroupProfile, path: str | None) -> GroupProfile | ScimFailure:
    if path is None:
        return ScimFailure(400, "noTarget")
    if _readonly(path.lower()) or path.lower() in {"externalid", "displayname"}:
        return ScimFailure(400, "mutability")
    if path.lower() == "members":
        return profile.model_copy(update={"members": ()})
    matched: Final = _MEMBER_PATH.fullmatch(path)
    if matched is None:
        return ScimFailure(400, "invalidPath")
    try:
        value: Final = json.loads(matched[1])
        if not isinstance(value, str):
            return ScimFailure(400, "invalidPath")
        member: Final = UUID(value)
    except ValueError:
        return ScimFailure(400, "invalidPath")
    return profile.model_copy(
        update={"members": tuple(identifier for identifier in profile.members if identifier != member)}
    )


def _operation(profile: GroupProfile, value: object) -> GroupProfile | ScimFailure:
    fields: Final = scim_object_fields(value)
    if isinstance(fields, ScimFailure):
        return fields
    operation: Final = fields.get("op")
    path: Final = fields.get("path")
    if (
        set(fields) - {"op", "path", "value"}
        or not isinstance(operation, str)
        or operation.lower() not in {"add", "replace", "remove"}
    ):
        return ScimFailure(400, "invalidSyntax")
    if path is not None and not isinstance(path, str):
        return ScimFailure(400, "invalidPath")
    if operation.lower() == "remove":
        if "value" in fields:
            return ScimFailure(400, "invalidSyntax")
        return _remove(profile, path)
    if "value" not in fields:
        return ScimFailure(400, "invalidValue")
    if path is None:
        changes: Final = scim_object_fields(fields["value"])
        return (
            changes if isinstance(changes, ScimFailure) else _change(profile, changes, add=operation.lower() == "add")
        )
    return _change(profile, {path.lower(): fields["value"]}, add=operation.lower() == "add")


def apply_group_patch(body: bytes, current: GroupRecord) -> GroupChange | ScimFailure:
    value: Final = decode_scim_json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = scim_object_fields(value)
    if isinstance(fields, ScimFailure):
        return fields
    operations: Final = fields.get("operations")
    if (
        set(fields) != {"schemas", "operations"}
        or not scim_schema_matches(fields.get("schemas"), PATCH_SCHEMA)
        or not isinstance(operations, list)
        or not 1 <= len(operations) <= 16
    ):
        return ScimFailure(400, "invalidSyntax")
    profile: GroupProfile = _current(current)  # rebind-ok: compute ordered immutable PATCH replacements
    removed: tuple[UUID, ...] = ()  # rebind-ok: keep every removed member even if later operations add it back
    for operation in operations:
        updated: Final = _operation(profile, operation)
        if isinstance(updated, ScimFailure):
            return updated
        disappeared: Final = tuple(
            member for member in profile.members if member not in updated.members and member not in removed
        )
        removed = (*removed, *disappeared)
        profile = updated
    return GroupChange(profile=profile, removed_members=removed)


def parse_group_replace(body: bytes, current: GroupRecord) -> GroupChange | ScimFailure:
    profile: Final = parse_group_create(body)
    if isinstance(profile, ScimFailure):
        return profile
    if profile.external_id != current.external_id:
        return ScimFailure(400, "mutability")
    return GroupChange(
        profile=profile, removed_members=tuple(member for member in current.members if member not in profile.members)
    )


def group_etag(record: GroupRecord) -> str:
    return f'W/"{record.id.hex}-{record.version}"'


def matches_group_etag(value: str | None, current: GroupRecord) -> bool | ScimFailure:
    if value is None or value == "*":
        return True
    if not SCIM_WEAK_ETAG.fullmatch(value):
        return ScimFailure(400, "invalidSyntax")
    return value == group_etag(current)


def render_group(record: GroupRecord, config: ScimConfig) -> JsonValue:
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(record.id),
        "externalId": record.external_id,
        "displayName": record.display_name,
        "members": [
            {"value": str(member), "$ref": f"{config.public_origin}{SCIM_BASE}/Users/{member}", "type": "User"}
            for member in record.members
        ],
        "meta": {
            "resourceType": "Group",
            "created": record.created_at.isoformat(),
            "lastModified": record.updated_at.isoformat(),
            "version": group_etag(record),
            "location": f"{config.public_origin}{SCIM_BASE}/Groups/{record.id}",
        },
    }


def render_group_list(page: GroupList, config: ScimConfig) -> JsonValue:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": page.total,
        "startIndex": page.start_index,
        "itemsPerPage": len(page.resources),
        "Resources": [render_group(row, config) for row in page.resources],
    }


def group_resource_type(config: ScimConfig) -> JsonValue:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "Group",
        "name": "Group",
        "endpoint": "/Groups",
        "schema": GROUP_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": f"{config.public_origin}{SCIM_BASE}/ResourceTypes/Group"},
    }


def group_schema_document(config: ScimConfig) -> JsonValue:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
        "id": GROUP_SCHEMA,
        "name": "Group",
        "attributes": [
            {
                "name": "displayName",
                "type": "string",
                "multiValued": False,
                "required": True,
                "caseExact": True,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
            },
            {
                "name": "members",
                "type": "complex",
                "multiValued": True,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
                "subAttributes": [
                    {"name": "value", "type": "string", "required": True, "mutability": "immutable", "caseExact": True},
                    {"name": "$ref", "type": "reference", "referenceTypes": ["User"], "mutability": "readOnly"},
                    {"name": "type", "type": "string", "canonicalValues": ["User"], "mutability": "readOnly"},
                    {"name": "display", "type": "string", "mutability": "readOnly"},
                ],
            },
        ],
        "meta": {"resourceType": "Schema", "location": f"{config.public_origin}{SCIM_BASE}/Schemas/{GROUP_SCHEMA}"},
    }

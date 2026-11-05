from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NoReturn
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
)

if TYPE_CHECKING:
    from .scim_config import ScimConfig

SCIM_BASE: Final = "/retail/scim/v2"
SCIM_USER_PREFIX: Final = "retail-scim-user:"
USER_SCHEMA: Final = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:Error"
SEARCH_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:SearchRequest"
_FILTER: Final = re.compile(r"\s*(id|externalId|userName|active)\s+eq\s+(.+?)\s*\Z", re.IGNORECASE | re.ASCII)
_ETAG: Final = re.compile(r'W/"[\x21\x23-\x7e]{1,128}"\Z')
_PROFILE_NAMES: Final = frozenset({"schemas", "externalid", "username", "displayname", "active", "id", "meta"})


@dataclass(frozen=True, slots=True)
class ScimFailure:
    status: int
    scim_type: str | None = None
    kind: Literal["failure"] = "failure"

    @property
    def detail(self) -> str:
        return {
            400: "SCIM request is invalid",
            401: "SCIM authentication failed",
            403: "SCIM operation is forbidden",
            404: "SCIM resource was not found",
            405: "SCIM method is unsupported",
            406: "SCIM representation is unsupported",
            409: "SCIM resource conflicts with current state",
            412: "SCIM resource version has changed",
            413: "SCIM request is too large",
            415: "SCIM content type is unsupported",
            429: "SCIM service is busy",
        }.get(self.status, "SCIM service is temporarily unavailable")


class ScimProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    external_id: str = Field(min_length=1, max_length=255, repr=False)
    user_name: str = Field(min_length=1, max_length=255, repr=False)
    display_name: str | None = Field(default=None, max_length=255, repr=False)
    active: StrictBool

    @field_validator("external_id", "user_name")
    @classmethod
    def ascii_identity(cls, value: str, info: ValidationInfo) -> str:
        minimum: Final = 33 if info.field_name == "user_name" else 32
        if any(ord(character) < minimum or ord(character) > 126 for character in value):
            raise ValueError("Invalid directory identifier")
        return value

    @field_validator("display_name")
    @classmethod
    def printable_display(cls, value: str | None) -> str | None:
        if value is not None and any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
            raise ValueError("Invalid directory display name")
        return value


class ScimRecord(ScimProfile):
    id: UUID
    user_id: str = Field(repr=False)
    namespace: str = Field(repr=False)
    version: StrictInt = Field(ge=1)
    credential_revision: UUID = Field(repr=False)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ScimList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    resources: tuple[ScimRecord, ...]
    total: StrictInt = Field(ge=0)
    start_index: StrictInt = Field(ge=1)


class ScimQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    start_index: StrictInt = Field(default=1, ge=1)
    count: StrictInt = Field(default=100, ge=0, le=100)
    filter_field: Literal["id", "external_id", "user_name", "active"] | None = None
    filter_value: str | bool | None = Field(default=None, repr=False)


class ScimChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    profile: ScimProfile = Field(repr=False)
    rotate_credentials: StrictBool


def _pairs(pairs: Sequence[tuple[str, object]]) -> Mapping[str, object]:
    if len(frozenset(key.lower() for key, _ in pairs)) != len(pairs):
        raise ValueError("Duplicate directory attribute")
    return dict(pairs)


def _invalid_number(value: str) -> NoReturn:
    raise ValueError("Invalid JSON number")


def _json(body: bytes) -> object:
    if len(body) > 65536:
        return ScimFailure(413)
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_number)
    except (UnicodeError, ValueError, RecursionError):
        return ScimFailure(400, "invalidSyntax")


def _object(value: object) -> Mapping[str, object] | ScimFailure:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return ScimFailure(400, "invalidSyntax")
    return {key.lower(): item for key, item in value.items()}


def _schema(value: object, expected: str) -> bool:
    return isinstance(value, list) and value == [expected]


def _profile(value: Mapping[str, object], *, explicit_active: bool) -> ScimProfile | ScimFailure:
    if set(value) - _PROFILE_NAMES or not _schema(value.get("schemas"), USER_SCHEMA):
        return ScimFailure(400, "invalidValue")
    if explicit_active and "active" not in value:
        return ScimFailure(400, "invalidValue")
    try:
        return ScimProfile.model_validate(
            {
                "external_id": value.get("externalid"),
                "user_name": value.get("username"),
                "display_name": value.get("displayname"),
                "active": value.get("active", True),
            }
        )
    except ValidationError:
        return ScimFailure(400, "invalidValue")


def parse_create(body: bytes) -> ScimProfile | ScimFailure:
    value: Final = _json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = _object(value)
    return fields if isinstance(fields, ScimFailure) else _profile(fields, explicit_active=False)


def _filter(value: str) -> ScimQuery | ScimFailure:
    matched: Final = _FILTER.fullmatch(value) if len(value) <= 2048 else None
    if matched is None:
        return ScimFailure(400, "invalidFilter")
    field: Final = {"id": "id", "externalid": "external_id", "username": "user_name", "active": "active"}[
        matched[1].lower()
    ]
    try:
        encoded: Final = matched[2].encode("utf-8")
    except UnicodeError:
        return ScimFailure(400, "invalidFilter")
    parsed: Final = _json(encoded)
    if field == "active":
        return (
            ScimQuery(filter_field="active", filter_value=parsed)
            if isinstance(parsed, bool)
            else ScimFailure(400, "invalidFilter")
        )
    if (
        not isinstance(parsed, str)
        or not 1 <= len(parsed) <= 255
        or any(ord(character) < 32 or ord(character) > 126 for character in parsed)
    ):
        return ScimFailure(400, "invalidFilter")
    return ScimQuery.model_validate(
        {"filter_field": field, "filter_value": parsed.lower() if field == "user_name" else parsed}
    )


def _query(values: Mapping[str, object]) -> ScimQuery | ScimFailure:
    if set(values) - {"startindex", "count", "filter"}:
        return ScimFailure(400, "invalidValue")
    try:
        start: Final = int(str(values.get("startindex", 1)))
        count: Final = int(str(values.get("count", 100)))
    except ValueError:
        return ScimFailure(400, "invalidValue")
    expression: Final = values.get("filter")
    if expression is not None and not isinstance(expression, str):
        return ScimFailure(400, "invalidFilter")
    filtered: Final = _filter(expression) if isinstance(expression, str) else ScimQuery()
    if isinstance(filtered, ScimFailure):
        return filtered
    return filtered.model_copy(update={"start_index": max(1, start), "count": min(100, max(0, count))})


def parse_query(params: Sequence[tuple[str, str]]) -> ScimQuery | ScimFailure:
    if len(params) > 3 or len(frozenset(name.lower() for name, _ in params)) != len(params):
        return ScimFailure(400, "invalidValue")
    return _query({name.lower(): value for name, value in params})


def parse_search(body: bytes) -> ScimQuery | ScimFailure:
    value: Final = _json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = _object(value)
    if isinstance(fields, ScimFailure):
        return fields
    if not _schema(fields.get("schemas"), SEARCH_SCHEMA):
        return ScimFailure(400, "invalidValue")
    if any(
        name in fields and (isinstance(fields[name], bool) or not isinstance(fields[name], int))
        for name in ("startindex", "count")
    ):
        return ScimFailure(400, "invalidValue")
    return _query({name: value for name, value in fields.items() if name != "schemas"})


def _current_profile(current: ScimRecord) -> ScimProfile:
    return ScimProfile(
        external_id=current.external_id,
        user_name=current.user_name,
        display_name=current.display_name,
        active=current.active,
    )


def _read_only(path: str) -> bool:
    return path in {"id", "meta"} or path.startswith("meta.")


def _patch_values(profile: ScimProfile, changes: Mapping[str, object]) -> ScimProfile | ScimFailure:
    if any(_read_only(name) for name in changes):
        return ScimFailure(400, "mutability")
    if set(changes) - {"externalid", "username", "displayname", "active"}:
        return ScimFailure(400, "invalidPath")
    if "externalid" in changes and changes["externalid"] != profile.external_id:
        return ScimFailure(400, "mutability")
    try:
        return ScimProfile.model_validate(
            {
                "external_id": profile.external_id,
                "user_name": changes.get("username", profile.user_name),
                "display_name": changes.get("displayname", profile.display_name),
                "active": changes.get("active", profile.active),
            }
        )
    except ValidationError:
        return ScimFailure(400, "invalidValue")


def _patch_operation(profile: ScimProfile, value: object) -> ScimProfile | ScimFailure:
    fields: Final = _object(value)
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
        if path is None:
            return ScimFailure(400, "noTarget")
        if isinstance(path, str) and _read_only(path.lower()):
            return ScimFailure(400, "mutability")
        if not isinstance(path, str) or path.lower() != "displayname":
            return ScimFailure(
                400, "mutability" if isinstance(path, str) and path.lower() == "externalid" else "invalidPath"
            )
        if "value" in fields:
            return ScimFailure(400, "invalidSyntax")
        return profile.model_copy(update={"display_name": None})
    if "value" not in fields:
        return ScimFailure(400, "invalidValue")
    if path is None:
        changes: Final = _object(fields["value"])
        return changes if isinstance(changes, ScimFailure) else _patch_values(profile, changes)
    return _patch_values(profile, {path.lower(): fields["value"]})


def apply_patch(body: bytes, current: ScimRecord) -> ScimChange | ScimFailure:
    value: Final = _json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = _object(value)
    if isinstance(fields, ScimFailure):
        return fields
    operations: Final = fields.get("operations")
    if (
        set(fields) != {"schemas", "operations"}
        or not _schema(fields.get("schemas"), PATCH_SCHEMA)
        or not isinstance(operations, list)
        or not 1 <= len(operations) <= 16
    ):
        return ScimFailure(400, "invalidSyntax")
    profile: ScimProfile = _current_profile(current)  # rebind-ok: apply ordered immutable replacements
    rotate: bool = False  # rebind-ok: preserve intermediate active transitions
    for operation in operations:
        updated: Final = _patch_operation(profile, operation)
        if isinstance(updated, ScimFailure):
            return updated
        rotate = rotate or updated.active != profile.active
        profile = updated
    return ScimChange(profile=profile, rotate_credentials=rotate)


def parse_replace(body: bytes, current: ScimRecord) -> ScimChange | ScimFailure:
    value: Final = _json(body)
    if isinstance(value, ScimFailure):
        return value
    fields: Final = _object(value)
    if isinstance(fields, ScimFailure):
        return fields
    profile: Final = _profile(fields, explicit_active=True)
    if isinstance(profile, ScimFailure):
        return profile
    if profile.external_id != current.external_id:
        return ScimFailure(400, "mutability")
    return ScimChange(profile=profile, rotate_credentials=profile.active != current.active)


def etag(record: ScimRecord) -> str:
    return f'W/"{record.id.hex}-{record.version}"'


def matches_etag(value: str | None, current: ScimRecord) -> bool | ScimFailure:
    if value is None or value == "*":
        return True
    if not _ETAG.fullmatch(value):
        return ScimFailure(400, "invalidSyntax")
    return value == etag(current)


def render_user(record: ScimRecord, config: ScimConfig) -> JsonValue:
    return {
        "schemas": [USER_SCHEMA],
        "id": str(record.id),
        "externalId": record.external_id,
        "userName": record.user_name,
        "active": record.active,
        **({"displayName": record.display_name} if record.display_name is not None else {}),
        "meta": {
            "resourceType": "User",
            "created": record.created_at.isoformat(),
            "lastModified": record.updated_at.isoformat(),
            "version": etag(record),
            "location": f"{config.public_origin}{SCIM_BASE}/Users/{record.id}",
        },
    }


def render_list(page: ScimList, config: ScimConfig) -> JsonValue:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": page.total,
        "startIndex": page.start_index,
        "itemsPerPage": len(page.resources),
        "Resources": [render_user(row, config) for row in page.resources],
    }


def render_error(failure: ScimFailure) -> JsonValue:
    return {
        "schemas": [ERROR_SCHEMA],
        "status": str(failure.status),
        "detail": failure.detail,
        **({"scimType": failure.scim_type} if failure.scim_type is not None else {}),
    }


def service_provider_config(config: ScimConfig) -> JsonValue:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 100},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": True},
        "authenticationSchemes": [
            {
                "name": "Bearer token",
                "type": "oauthbearertoken",
                "description": "Server-configured opaque provisioning token",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{config.public_origin}{SCIM_BASE}/ServiceProviderConfig",
        },
    }


def resource_types(config: ScimConfig) -> JsonValue:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": USER_SCHEMA,
                "meta": {
                    "resourceType": "ResourceType",
                    "location": f"{config.public_origin}{SCIM_BASE}/ResourceTypes/User",
                },
            }
        ],
    }


def schema_documents(config: ScimConfig) -> JsonValue:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
                "id": USER_SCHEMA,
                "name": "User",
                "attributes": [
                    _attribute("userName", "string", True, False, "readWrite", "server"),
                    _attribute("displayName", "string", False, False, "readWrite", "none"),
                    _attribute("active", "boolean", False, False, "readWrite", "none"),
                ],
                "meta": {
                    "resourceType": "Schema",
                    "location": f"{config.public_origin}{SCIM_BASE}/Schemas/{USER_SCHEMA}",
                },
            }
        ],
    }


def _attribute(name: str, kind: str, required: bool, case_exact: bool, mutability: str, uniqueness: str) -> JsonValue:
    return {
        "name": name,
        "type": kind,
        "multiValued": False,
        "required": required,
        "caseExact": case_exact,
        "mutability": mutability,
        "returned": "default",
        "uniqueness": uniqueness,
    }

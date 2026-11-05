import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .oidc_config import valid_oidc_url

_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SCOPE: Final = re.compile(r"[A-Za-z0-9:._/-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class ApiJwtFailure:
    code: Literal["invalid", "unavailable"]
    kind: Literal["failure"] = "failure"


class ApiJwtBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    subject: str = Field(min_length=1, max_length=255, repr=False)
    user_id: str = Field(min_length=1, max_length=191, repr=False)
    token_hash: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    binding_revision: UUID
    enabled: StrictBool = True

    @field_validator("subject", "user_id")
    @classmethod
    def bounded_identifier(cls, value: str) -> str:
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("Invalid API identity binding")
        return value


class ApiJwtConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    namespace: str
    generation: StrictInt = Field(ge=1, le=9007199254740991)
    issuer: str = Field(min_length=1, max_length=2048, repr=False)
    audience: str = Field(min_length=1, max_length=512, repr=False)
    client_id: str = Field(min_length=1, max_length=512, repr=False)
    jwks_uri: str = Field(min_length=1, max_length=2048, repr=False)
    required_scope: str = "models:invoke"
    max_token_seconds: StrictInt = Field(default=300, ge=60, le=900)
    bindings: tuple[ApiJwtBinding, ...] = Field(min_length=1, max_length=128, repr=False)
    allow_loopback_http: StrictBool = False

    @field_validator("namespace")
    @classmethod
    def valid_namespace(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Invalid API identity namespace")
        return value

    @field_validator("required_scope")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        if not _SCOPE.fullmatch(value):
            raise ValueError("Invalid API identity scope")
        return value

    @field_validator("audience", "client_id")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("Invalid API identity identifier")
        return value

    @model_validator(mode="after")
    def fixed_configuration(self) -> Self:
        valid_oidc_url(self.issuer, loopback=self.allow_loopback_http)
        valid_oidc_url(self.jwks_uri, loopback=self.allow_loopback_http)
        if self.allow_loopback_http and any(
            urlsplit(value).hostname not in ("localhost", "127.0.0.1", "::1")
            for value in (self.issuer, self.jwks_uri)
        ):
            raise ValueError("API identity test mode requires loopback endpoints")
        if len(frozenset(row.subject for row in self.bindings)) != len(self.bindings):
            raise ValueError("Duplicate API identity subject")
        if len(frozenset(row.binding_revision for row in self.bindings)) != len(self.bindings):
            raise ValueError("Duplicate API identity binding revision")
        return self

    def digest(self) -> str:
        body: Final = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


def load_api_jwt_config(environ: Mapping[str, str] | None = None) -> ApiJwtConfig | ApiJwtFailure | None:
    source: Final = os.environ if environ is None else environ
    if source.get("RETAIL_API_JWT_ENABLED", "false").lower() != "true":
        return None
    try:
        with Path(source["RETAIL_API_JWT_CONFIG"]).open("rb") as config_file:
            body: Final = config_file.read(131073)
        if len(body) > 131072:
            return ApiJwtFailure("invalid")
        return ApiJwtConfig.model_validate_json(body)
    except (KeyError, OSError, ValueError):
        return ApiJwtFailure("invalid")

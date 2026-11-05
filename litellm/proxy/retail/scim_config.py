import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, field_validator, model_validator

from .oidc_config import valid_oidc_url
from .scim_protocol import ScimFailure

_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TOKEN: Final = re.compile(r"rt-scim\.[A-Za-z0-9_-]{32,256}\Z")


class ScimConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    namespace: str
    generation: StrictInt = Field(ge=1, le=9007199254740991)
    public_origin: str = Field(min_length=1, max_length=2048, repr=False)
    max_users: StrictInt = Field(default=1000, ge=1, le=10000)
    max_groups: StrictInt = Field(default=1000, ge=1, le=1000)
    max_memberships: StrictInt = Field(default=100000, ge=1, le=100000)
    access_group_external_ids: tuple[str, ...] = Field(default=(), max_length=128, repr=False)
    allow_loopback_http: StrictBool = False

    @field_validator("access_group_external_ids")
    @classmethod
    def explicit_access_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(frozenset(value)) != len(value) or any(
            not 1 <= len(identifier) <= 255
            or any(ord(character) < 32 or ord(character) > 126 for character in identifier)
            for identifier in value
        ):
            raise ValueError("Invalid directory access groups")
        return value

    @field_validator("namespace")
    @classmethod
    def bounded_namespace(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Invalid directory namespace")
        return value

    @model_validator(mode="after")
    def exact_origin(self) -> Self:
        valid_oidc_url(self.public_origin, loopback=self.allow_loopback_http, origin_only=True)
        if (
            self.public_origin.endswith("/")
            or self.allow_loopback_http
            and urlsplit(self.public_origin).hostname not in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Invalid directory origin")
        return self


@dataclass(frozen=True, slots=True)
class ScimSetup:
    config: ScimConfig
    token: SecretStr = field(repr=False)

    def digest(self) -> str:
        payload: Final = {
            "config": self.config.model_dump(mode="json"),
            "token_hash": hashlib.sha256(self.token.get_secret_value().encode()).hexdigest(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique_pairs(pairs: Sequence[tuple[str, object]]) -> Mapping[str, object]:
    if len(frozenset(name for name, _ in pairs)) != len(pairs):
        raise ValueError("Duplicate directory configuration")
    return dict(pairs)


def load_scim_setup(environ: Mapping[str, str] | None = None) -> ScimSetup | ScimFailure | None:
    values: Final = os.environ if environ is None else environ
    if values.get("RETAIL_SCIM_ENABLED", "false").lower() != "true":
        return None
    try:
        token: Final = values["RETAIL_SCIM_TOKEN"]
        if not _TOKEN.fullmatch(token):
            return ScimFailure(503)
        with Path(values["RETAIL_SCIM_CONFIG"]).open("rb") as stream:
            body: Final = stream.read(131073)
        if len(body) > 131072:
            return ScimFailure(503)
        config: Final = ScimConfig.model_validate(json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs))
        return ScimSetup(config, SecretStr(token))
    except (KeyError, OSError, ValueError, RecursionError):
        return ScimFailure(503)

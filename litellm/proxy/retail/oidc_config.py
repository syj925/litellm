import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ENV: Final = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_PATH: Final = re.compile(r"/[A-Za-z0-9_./-]*\Z")
_QUERY_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True, slots=True)
class OidcFailure:
    code: Literal["invalid", "unavailable", "denied"]
    kind: Literal["failure"] = "failure"


class OidcBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    subject: str = Field(min_length=1, max_length=255, repr=False)
    user_id: str = Field(min_length=1, max_length=191, repr=False)
    enabled: StrictBool = True

    @field_validator("subject", "user_id")
    @classmethod
    def bounded_identifier(cls, value: str) -> str:
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("Invalid identity binding")
        return value


def valid_oidc_url(value: str, *, loopback: bool, origin_only: bool = False) -> str:
    parts: Final = urlsplit(value)
    port: Final = parts.port
    if (
        not parts.hostname
        or parts.scheme not in ("https", "http")
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (origin_only and parts.path not in ("", "/"))
        or (port is not None and port < 1)
        or "\\" in value
        or "%" in value
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Invalid identity endpoint")
    if parts.scheme == "http" and not (loopback and parts.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Identity endpoint requires TLS")
    return value


def url_origin(value: str) -> str:
    parts: Final = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc}"


class OidcConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    namespace: str
    generation: StrictInt = Field(ge=1, le=9007199254740991)
    issuer: str = Field(repr=False)
    client_id: str = Field(min_length=1, max_length=512, repr=False)
    client_secret_env: str = "RETAIL_OIDC_CLIENT_SECRET"
    client_auth_method: Literal["client_secret_basic", "client_secret_post"]
    public_origin: str = Field(repr=False)
    endpoint_origins: tuple[str, ...] = Field(default=(), max_length=4, repr=False)
    label: str = Field(default="Organization sign in", min_length=1, max_length=80)
    bindings: tuple[OidcBinding, ...] = Field(min_length=1, max_length=128, repr=False)
    session_seconds: StrictInt = Field(default=1800, ge=300, le=3600)
    allow_loopback_http: StrictBool = False

    @field_validator("namespace")
    @classmethod
    def valid_namespace(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Invalid identity namespace")
        return value

    @field_validator("client_secret_env")
    @classmethod
    def valid_environment(cls, value: str) -> str:
        if not _ENV.fullmatch(value):
            raise ValueError("Invalid identity environment name")
        return value

    @field_validator("client_id", "label")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Invalid identity text")
        return value

    @model_validator(mode="after")
    def fixed_configuration(self) -> Self:
        valid_oidc_url(self.issuer, loopback=self.allow_loopback_http)
        valid_oidc_url(self.public_origin, loopback=self.allow_loopback_http, origin_only=True)
        for endpoint_origin in self.endpoint_origins:
            valid_oidc_url(endpoint_origin, loopback=self.allow_loopback_http, origin_only=True)
            if endpoint_origin.endswith("/"):
                raise ValueError("Identity endpoint origins must be exact")
        if len(self.allowed_endpoint_origins()) > 4:
            raise ValueError("Too many identity endpoint origins")
        if self.public_origin.endswith("/"):
            raise ValueError("Identity origins must use their registered exact form")
        if len(frozenset(binding.subject for binding in self.bindings)) != len(self.bindings):
            raise ValueError("Duplicate identity subject")
        if self.allow_loopback_http and any(
            urlsplit(value).hostname not in ("localhost", "127.0.0.1", "::1")
            for value in (self.issuer, self.public_origin, *self.endpoint_origins)
        ):
            raise ValueError("Identity test mode requires loopback endpoints")
        return self

    def digest(self) -> str:
        body: Final = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def callback_uri(self) -> str:
        return f"{self.public_origin}/retail/auth/oidc/callback"

    def allowed_endpoint_origins(self) -> frozenset[str]:
        return frozenset((url_origin(self.issuer), *self.endpoint_origins))

    def binding(self, subject: str) -> OidcBinding | None:
        return next((binding for binding in self.bindings if binding.subject == subject and binding.enabled), None)


def safe_return_to(config: OidcConfig, value: str) -> str | OidcFailure:
    if len(value) > 2048 or "\\" in value or any(ord(character) < 33 or ord(character) > 126 for character in value):
        return OidcFailure("invalid")
    try:
        parts: Final = urlsplit(value)
        path: Final = unquote(parts.path, errors="strict")
        query: Final = tuple(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16))
    except (ValueError, UnicodeError):
        return OidcFailure("invalid")
    if (
        parts.fragment
        or parts.username is not None
        or parts.password is not None
        or (parts.netloc and url_origin(value) != config.public_origin)
        or (parts.scheme and not parts.netloc)
        or value.startswith("//")
        or not (path == "/ui" or path.startswith("/ui/"))
        or not _PATH.fullmatch(path)
        or "//" in path
        or any(segment in (".", "..") for segment in path.split("/"))
        or any(segment in ("login", "logout", "callback") for segment in path.split("/"))
        or any(not _QUERY_NAME.fullmatch(name) or "redirect" in name.lower() for name, _ in query)
        or any(any(ord(character) < 32 or ord(character) == 127 for character in item) for _, item in query)
        or any("%" in item or "\\" in item for _, item in query)
    ):
        return OidcFailure("invalid")
    return path + (f"?{parts.query}" if parts.query else "")


def load_oidc_config(environ: Mapping[str, str] | None = None) -> OidcConfig | OidcFailure | None:
    source: Final = os.environ if environ is None else environ
    if source.get("RETAIL_OIDC_SSO_ENABLED", "false").lower() != "true":
        return None
    try:
        with Path(source["RETAIL_OIDC_SSO_CONFIG"]).open("rb") as config_file:
            body: Final = config_file.read(131073)
        if len(body) > 131072:
            return OidcFailure("invalid")
        return OidcConfig.model_validate_json(body)
    except (KeyError, OSError, ValueError):
        return OidcFailure("invalid")

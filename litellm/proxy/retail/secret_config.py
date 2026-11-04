import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

MANAGED_SECRET_PREFIX: Final = "retail-secret/"
_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,95}\Z")
_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\Z")


def valid_secret_path(value: str) -> str:
    parts: Final = value.split("/")
    if len(value) > 512 or not parts or any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("Invalid managed secret path")
    return value


class SecretBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    ref: str
    path: str = Field(repr=False)
    field: str = "api_key"

    @field_validator("ref", "field")
    @classmethod
    def name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Invalid managed secret name")
        return value

    @field_validator("path")
    @classmethod
    def path_name(cls, value: str) -> str:
        return valid_secret_path(value)


class ManagedSecretsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    endpoint: str = Field(repr=False)
    token_env: str = "RETAIL_VAULT_TOKEN"
    mount: str = "secret"
    namespace: str | None = Field(default=None, repr=False)
    refs: tuple[SecretBinding, ...] = Field(min_length=1, max_length=128)
    ttl_seconds: StrictInt = Field(default=60, ge=1, le=3600)
    refresh_seconds: float = Field(default=15, ge=0.1, le=900, allow_inf_nan=False)
    timeout_seconds: float = Field(default=2, ge=0.1, le=10, allow_inf_nan=False)
    retries: StrictInt = Field(default=1, ge=0, le=2)
    concurrency: StrictInt = Field(default=4, ge=1, le=8)
    max_response_bytes: StrictInt = Field(default=65536, ge=1024, le=1048576)
    allow_loopback_http: StrictBool = False

    @field_validator("token_env")
    @classmethod
    def environment_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("Invalid managed secret environment name")
        return value

    @field_validator("mount")
    @classmethod
    def mount_name(cls, value: str) -> str:
        return valid_secret_path(value)

    @field_validator("namespace")
    @classmethod
    def namespace_name(cls, value: str | None) -> str | None:
        return None if value is None else valid_secret_path(value)

    @model_validator(mode="after")
    def bounded_configuration(self) -> Self:
        endpoint: Final = urlsplit(self.endpoint)
        port: Final = endpoint.port
        if (
            not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in ("", "/")
            or endpoint.scheme not in ("http", "https")
            or (port is not None and port < 1)
            or any(ord(character) < 33 or ord(character) > 126 for character in self.endpoint)
        ):
            raise ValueError("Invalid managed secret endpoint")
        if endpoint.scheme == "http" and not (
            self.allow_loopback_http and endpoint.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Managed secret endpoint requires TLS")
        if self.refresh_seconds > self.ttl_seconds / 2:
            raise ValueError("Managed secret refresh must precede expiry")
        batches: Final = (len(self.refs) + self.concurrency - 1) // self.concurrency
        request_budget: Final = (self.retries + 1) * self.timeout_seconds + 0.05 * sum(range(self.retries + 1))
        if batches * request_budget + self.refresh_seconds >= self.ttl_seconds:
            raise ValueError("Managed secret refresh budget must fit within expiry")
        if len(frozenset(binding.ref for binding in self.refs)) != len(self.refs):
            raise ValueError("Managed secret references must be unique")
        return self


def load_secret_config(environ: Mapping[str, str] | None = None) -> ManagedSecretsConfig | None:
    source: Final = os.environ if environ is None else environ
    if source.get("RETAIL_MANAGED_SECRETS_ENABLED", "false").lower() != "true":
        return None
    try:
        file_path: Final = Path(source["RETAIL_MANAGED_SECRETS_CONFIG"])
        with file_path.open("rb") as config_file:
            body: Final = config_file.read(131073)
        if len(body) > 131072:
            raise ValueError("Managed secret configuration is too large")
        return ManagedSecretsConfig.model_validate(json.loads(body))
    except (KeyError, OSError, ValueError):
        raise ValueError("Managed secret configuration is invalid") from None

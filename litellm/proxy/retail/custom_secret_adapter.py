import asyncio
import os
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastapi import HTTPException
from pydantic import ConfigDict, SecretStr, TypeAdapter, ValidationError

from .secret_cache import ManagedSecretCache
from .secret_config import MANAGED_SECRET_PREFIX, load_secret_config
from .secret_provider import VaultKV2Provider


class ManagedSecretUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=503, detail="Managed credential is unavailable")


class ManagedSecretOverrideRejected(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=400, detail="Managed credential routing overrides are not allowed")


class ManagedCredentialAdapter:
    def __init__(self, cache: ManagedSecretCache | None = None) -> None:
        self.cache = cache

    def read(self, ref: str) -> str:
        value: Final = self.cache.read(ref) if self.cache is not None else None
        if value is None:
            raise ManagedSecretUnavailable()
        return value.value.get_secret_value()

    async def async_read(self, ref: str) -> str:
        return self.read(ref)


_RUNTIME: Final = ManagedCredentialAdapter()
_OVERRIDES: Final = frozenset(
    ("base_url", "custom_llm_provider", "litellm_credential_name", "api_version")
)
_HEADER_FIELDS: Final = frozenset(("headers", "extra_headers", "default_headers"))
_PARAMS: Final = TypeAdapter(Mapping[str, object], config=ConfigDict(hide_input_in_errors=True))


def _params(deployment: Mapping[str, object]) -> Mapping[str, object]:
    raw: Final = deployment.get("litellm_params")
    try:
        return _PARAMS.validate_python(raw)
    except ValidationError:
        return MappingProxyType({})


def is_managed_deployment(deployment: Mapping[str, object]) -> bool:
    key: Final = _params(deployment).get("api_key")
    return isinstance(key, str) and key.startswith(MANAGED_SECRET_PREFIX)


def managed_credential_kwargs(
    deployment: Mapping[str, object],
    kwargs: Mapping[str, object],
    *,
    adapter: ManagedCredentialAdapter | None = None,
    final: bool = False,
) -> Mapping[str, object]:
    if not is_managed_deployment(deployment):
        requested_key: Final = kwargs.get("api_key")
        if isinstance(requested_key, str) and requested_key.startswith(MANAGED_SECRET_PREFIX):
            raise ManagedSecretOverrideRejected()
        return MappingProxyType({})
    params: Final = _params(deployment)
    key: Final = params.get("api_key")
    api_base: Final = params.get("api_base")
    model: Final = params.get("model")
    if (
        not isinstance(model, str)
        or not model.startswith("openai/")
        or params.get("custom_llm_provider") not in (None, "openai")
    ):
        raise ManagedSecretOverrideRejected()
    if any(
        key in kwargs and kwargs[key] is not None and not (final and kwargs[key] == params.get(key))
        for key in _OVERRIDES
    ):
        raise ManagedSecretOverrideRejected()
    if kwargs.get("api_key") not in (None, key) or kwargs.get("api_base") not in (None, api_base):
        raise ManagedSecretOverrideRejected()
    if kwargs.get("client") is not None:
        raise ManagedSecretOverrideRejected()
    if any(key in kwargs and kwargs[key] is not None for key in _HEADER_FIELDS):
        raise ManagedSecretOverrideRejected()
    if not isinstance(key, str) or not isinstance(api_base, str) or not api_base:
        raise ManagedSecretUnavailable()
    reader: Final = _RUNTIME if adapter is None else adapter
    value: Final = reader.read(key.removeprefix(MANAGED_SECRET_PREFIX))
    return MappingProxyType({"api_key": value, "api_base": api_base})


async def start_managed_secrets() -> None:
    if _RUNTIME.cache is not None:
        return
    config: Final = load_secret_config()
    if config is None:
        return
    token: Final = os.environ.get(config.token_env, "")
    if not token or len(token) > 8192 or any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise RuntimeError("Managed credential provider authentication is unavailable")
    provider: Final = VaultKV2Provider(config, SecretStr(token), pause=asyncio.sleep)
    cache: Final = ManagedSecretCache(config, provider, clock=time.monotonic)
    await cache.start()
    _RUNTIME.cache = cache


async def close_managed_secrets() -> None:
    cache: Final = _RUNTIME.cache
    _RUNTIME.cache = None
    if cache is not None:
        await cache.close()

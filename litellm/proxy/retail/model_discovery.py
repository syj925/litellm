from collections.abc import Mapping
from typing import Final, Literal, TypeAlias

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPResponseLimitError
from litellm.secret_managers.main import get_secret_str
from litellm.utils import add_path_to_api_base

from .api import Admin

Protocol: TypeAlias = Literal["openai", "anthropic", "gemini", "ollama"]
_MAX_MODELS: Final = 5000
_MAX_PAGES: Final = 5
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_DEFAULT_BASES: Final = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://localhost:11434",
}
router: Final = APIRouter(prefix="/retail/admin", tags=["Model discovery"])


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    provider: str = Field(min_length=1, max_length=100)
    api_base: str | None = Field(default=None, max_length=2048, alias="upstream_api_base")
    api_key: SecretStr | None = Field(default=None, max_length=8192, alias="upstream_api_key")
    credential_name: str | None = Field(default=None, min_length=1, max_length=200)
    protocol: Protocol | None = None

    @field_validator("api_key")
    @classmethod
    def validate_header_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and any(char in value.get_secret_value() for char in ("\n", "\r", "\x00")):
            raise ValueError("API keys must not contain control characters")
        return value


class ModelDiscoveryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    models: tuple[str, ...]
    protocol: Protocol
    source: str
    count: int
    truncated: bool


class DiscoveryTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    protocol: Protocol
    api_key: SecretStr | None


class _RemoteModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    name: str | None = None


class _ModelPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: tuple[_RemoteModel, ...] | None = None
    models: tuple[_RemoteModel, ...] | None = None
    has_more: bool = False
    last_id: str | None = None
    nextPageToken: str | None = None


def discovery_protocol(provider: str) -> Protocol:
    normalized: Final = provider.strip().lower().replace("-", "_")
    if normalized in {"anthropic", "anthropic_text"}:
        return "anthropic"
    if normalized in {"gemini", "google_ai_studio", "google ai studio"}:
        return "gemini"
    if normalized in {"ollama", "ollama_chat"}:
        return "ollama"
    return "openai"


def discovery_url(api_base: str, protocol: Protocol) -> str:
    try:
        parsed: Final = httpx.URL(api_base.strip())
    except (httpx.InvalidURL, ValueError) as exc:
        raise HTTPException(400, "Enter a valid upstream API base URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo or parsed.query or parsed.fragment:
        raise HTTPException(400, "API base must be an HTTP(S) URL without credentials, query parameters, or fragments")
    path: Final = parsed.path.rstrip("/")
    if protocol == "ollama":
        base: Final = str(parsed.copy_with(path=path[:-3])) if path.endswith("/v1") else str(parsed)
        return add_path_to_api_base(base, "/api/tags")
    if path.endswith("/models"):
        return str(parsed.copy_with(path=path))
    version: Final = path.rsplit("/", 1)[-1]
    if version in {"v1", "v1alpha", "v1beta", "v2", "v2beta"}:
        return add_path_to_api_base(str(parsed), "/models")
    return add_path_to_api_base(str(parsed), "/v1beta/models" if protocol == "gemini" else "/v1/models")


def _credential_string(values: Mapping[str, object], key: str) -> str | None:
    value: Final = values.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    if value.startswith("os.environ/"):
        try:
            return get_secret_str(value)
        except (KeyError, ValueError, OSError) as exc:
            raise HTTPException(400, "The saved credential's environment reference could not be resolved") from exc
    return value


def _provider_default_base(provider: str | None) -> str | None:
    normalized: Final = provider.strip().lower().replace("-", "_") if provider else ""
    native_provider: Final = normalized in {
        "openai",
        "anthropic",
        "anthropic_text",
        "gemini",
        "google_ai_studio",
        "google ai studio",
        "ollama",
        "ollama_chat",
    }
    return _DEFAULT_BASES[discovery_protocol(normalized)] if native_provider else None


def resolve_target(
    data: ModelDiscoveryRequest,
    credential_values: Mapping[str, object] | None = None,
    credential_provider: str | None = None,
) -> DiscoveryTarget:
    protocol: Final = data.protocol or discovery_protocol(data.provider)
    default_base: Final = _provider_default_base(
        credential_provider if credential_values is not None else data.provider
    )
    saved_base: Final = _credential_string(credential_values or {}, "api_base")
    base: Final = saved_base or data.api_base or default_base
    if base is None:
        raise HTTPException(400, "Enter the upstream API base URL before fetching models")
    url: Final = discovery_url(base, protocol)
    if credential_values is not None:
        trusted_base: Final = saved_base or default_base
        if trusted_base is None:
            raise HTTPException(400, "This saved credential needs its own API base URL before model discovery")
        if data.api_base and discovery_url(data.api_base, protocol) != discovery_url(trusted_base, protocol):
            raise HTTPException(
                400, "The API base must match the saved credential; enter a new API key to use another upstream"
            )
    supplied_key: Final = data.api_key.get_secret_value() if data.api_key is not None else None
    saved_key: Final = _credential_string(credential_values or {}, "api_key")
    key: Final = saved_key if credential_values is not None else supplied_key
    if key is not None and any(char in key for char in ("\n", "\r", "\x00")):
        raise HTTPException(400, "The upstream API key cannot be used as an HTTP header")
    if not key and protocol != "ollama" and saved_base is None and not data.api_base:
        raise HTTPException(400, "Enter an upstream API key or choose an existing credential")
    return DiscoveryTarget(url=url, protocol=protocol, api_key=SecretStr(key) if key else None)


def _headers(target: DiscoveryTarget) -> dict[str, str]:
    key: Final = target.api_key.get_secret_value() if target.api_key is not None else None
    auth: Final = (
        {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if target.protocol == "anthropic" and key
        else {"x-goog-api-key": key}
        if target.protocol == "gemini" and key
        else {"Authorization": "Bearer " + key}
        if key
        else {}
    )
    return {"Accept": "application/json", **auth}


def _page_ids(page: _ModelPage, protocol: Protocol) -> tuple[str, ...]:
    entries: Final = page.models if protocol in {"gemini", "ollama"} else page.data
    if entries is None:
        raise HTTPException(502, "The upstream did not return a supported model list")
    raw: Final = tuple(entry.name if protocol in {"gemini", "ollama"} else entry.id for entry in entries)
    valid: Final = tuple(value.strip() for value in raw if isinstance(value, str) and 0 < len(value.strip()) <= 512)
    normalized: Final = tuple(value.removeprefix("models/") for value in valid) if protocol == "gemini" else valid
    return tuple(value for value in normalized if value and not any(char in value for char in ("\n", "\r", "\x00")))


async def _read_page(target: DiscoveryTarget, client: AsyncHTTPHandler, params: Mapping[str, str]) -> _ModelPage:
    try:
        response: Final = await client.get(
            url=target.url,
            params=dict(params),
            headers=_headers(target),
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "The upstream model list request timed out") from exc
    except HTTPResponseLimitError as exc:
        raise HTTPException(502, "The upstream model list response exceeded the supported size limit") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "Could not connect to the upstream model list endpoint") from exc
    if response.status_code == 401:
        raise HTTPException(400, "Upstream authentication failed (HTTP 401). Check the API key")
    if response.status_code == 403:
        raise HTTPException(400, "Upstream denied model-list permission (HTTP 403). Check the key's access")
    if response.status_code == 404:
        raise HTTPException(
            400, "The upstream does not expose this model-list endpoint. Check the API base or use manual entry"
        )
    if 300 <= response.status_code < 400:
        raise HTTPException(
            400, "The upstream returned a redirect. Enter its final API base URL; credentials were not forwarded"
        )
    if response.status_code == 429:
        raise HTTPException(429, "The upstream rate-limited model discovery. Try again later")
    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(502, "The upstream could not provide its model list")
    try:
        return _ModelPage.model_validate_json(response.content)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(502, "The upstream returned an invalid model list") from exc


async def _discover_pages(
    target: DiscoveryTarget,
    client: AsyncHTTPHandler,
    collected: tuple[str, ...],
    params: Mapping[str, str],
    pages_left: int,
) -> ModelDiscoveryResponse:
    page: Final = await _read_page(target, client, params)
    ids: Final = _page_ids(page, target.protocol)
    combined: Final = tuple(dict.fromkeys((*collected, *ids)))
    next_token: Final = (
        page.nextPageToken
        if target.protocol == "gemini"
        else (page.last_id or (ids[-1] if ids else None))
        if target.protocol == "anthropic" and page.has_more
        else None
    )
    more: Final = bool(page.nextPageToken) if target.protocol == "gemini" else page.has_more
    if more and next_token and pages_left > 1 and len(combined) < _MAX_MODELS:
        parameter: Final = "pageToken" if target.protocol == "gemini" else "after_id"
        return await _discover_pages(target, client, combined, {parameter: next_token}, pages_left - 1)
    models: Final = combined[:_MAX_MODELS]
    return ModelDiscoveryResponse(
        models=models,
        protocol=target.protocol,
        source=target.url,
        count=len(models),
        truncated=more or len(combined) > _MAX_MODELS,
    )


async def discover_models(target: DiscoveryTarget, client: AsyncHTTPHandler) -> ModelDiscoveryResponse:
    return await _discover_pages(target, client, (), {}, _MAX_PAGES)


@router.post("/model-discovery", response_model=ModelDiscoveryResponse)
async def model_discovery(data: ModelDiscoveryRequest, admin: Admin) -> ModelDiscoveryResponse:
    credential: Final = CredentialAccessor.find_credential(data.credential_name) if data.credential_name else None
    credential_values: Final = (
        CredentialAccessor.get_credential_values(data.credential_name) if data.credential_name else None
    )
    if data.credential_name and credential is None:
        raise HTTPException(404, "The selected upstream credential no longer exists")
    stored_provider: Final = credential.credential_info.get("custom_llm_provider") if credential is not None else None
    target: Final = resolve_target(
        data, credential_values, stored_provider if isinstance(stored_provider, str) else None
    )
    client: Final = AsyncHTTPHandler(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)
    try:
        return await discover_models(target, client)
    finally:
        await client.close()

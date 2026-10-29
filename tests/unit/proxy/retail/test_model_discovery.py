from typing import Final

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.auth.auth_utils import is_request_body_safe
from litellm.proxy.retail.model_discovery import (
    DiscoveryTarget,
    ModelDiscoveryRequest,
    discover_models,
    discovery_url,
    resolve_target,
)


def test_admin_discovery_uses_scoped_upstream_fields_without_enabling_inference_passthrough():
    body: Final = {
        "provider": "openai",
        "upstream_api_base": "https://upstream.example/custom/v1",
        "upstream_api_key": "test-credential",
    }
    request: Final = ModelDiscoveryRequest.model_validate(body)
    assert request.api_base == body["upstream_api_base"]
    assert request.api_key is not None and request.api_key.get_secret_value() == body["upstream_api_key"]
    assert is_request_body_safe(body, {}, None, "", route="/retail/admin/model-discovery") is True
    with pytest.raises(ValueError, match="api_base is not allowed"):
        is_request_body_safe({"api_base": body["upstream_api_base"]}, {}, None, "", route="/v1/chat/completions")


@pytest.mark.parametrize(
    ("base", "protocol", "expected"),
    (
        ("https://upstream.example/custom/v1", "openai", "https://upstream.example/custom/v1/models"),
        ("https://upstream.example/custom/v1/", "anthropic", "https://upstream.example/custom/v1/models"),
        ("https://upstream.example/custom", "openai", "https://upstream.example/custom/v1/models"),
        ("https://upstream.example/custom/v1/models", "openai", "https://upstream.example/custom/v1/models"),
        ("https://upstream.example/custom/v1beta", "gemini", "https://upstream.example/custom/v1beta/models"),
        ("https://upstream.example/custom/v1", "gemini", "https://upstream.example/custom/v1/models"),
        ("http://localhost:11434/v1", "ollama", "http://localhost:11434/api/tags"),
        ("http://localhost:11434/custom/api", "ollama", "http://localhost:11434/custom/api/tags"),
    ),
)
def test_discovery_preserves_custom_paths_without_duplicate_version_segments(base, protocol, expected):
    assert discovery_url(base, protocol) == expected


@pytest.mark.parametrize(
    "base",
    (
        "file:///tmp/key",
        "https://user:password@upstream.example",
        "https://upstream.example?key=secret",
        "https://upstream.example#secret",
    ),
)
def test_discovery_does_not_accept_credentials_or_secrets_in_the_url(base: str):
    with pytest.raises(HTTPException) as failure:
        discovery_url(base, "openai")
    assert failure.value.status_code == 400


def test_saved_credential_cannot_be_redirected_to_an_overridden_upstream():
    request: Final = ModelDiscoveryRequest(
        provider="openai", api_base="https://other.example/v1", credential_name="saved"
    )
    with pytest.raises(HTTPException) as failure:
        resolve_target(request, {"api_base": "https://saved.example/v1", "api_key": "saved-secret"})
    assert failure.value.status_code == 400
    assert "saved-secret" not in str(failure.value.detail)


def test_saved_credential_without_base_keeps_its_provider_host_even_with_protocol_override():
    request: Final = ModelDiscoveryRequest(provider="anthropic", protocol="openai", credential_name="saved")
    target: Final = resolve_target(request, {"api_key": "saved-secret"}, "anthropic")
    assert httpx.URL(target.url).host == "api.anthropic.com"
    with pytest.raises(HTTPException):
        resolve_target(
            request.model_copy(update={"api_base": "https://api.openai.com/v1"}),
            {"api_key": "saved-secret"},
            "anthropic",
        )


def test_stored_base_equivalent_version_is_allowed_and_stored_key_wins():
    request: Final = ModelDiscoveryRequest(
        provider="openai",
        api_base="https://saved.example/custom/v1",
        credential_name="saved",
        api_key="new-secret",
    )
    target: Final = resolve_target(request, {"api_base": "https://saved.example/custom", "api_key": "saved-secret"})
    assert target.url == "https://saved.example/custom/v1/models"
    assert target.api_key.get_secret_value() == "saved-secret"
    assert "saved-secret" not in repr(target)


def test_saved_custom_credential_without_its_own_base_fails_closed():
    request: Final = ModelDiscoveryRequest(
        provider="openai_compatible", api_base="https://other.example", credential_name="saved"
    )
    with pytest.raises(HTTPException):
        resolve_target(request, {"api_key": "saved-secret"})


def test_saved_environment_reference_uses_native_secret_resolution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISCOVERY_TEST_API_KEY", "test-environment-value")
    target: Final = resolve_target(
        ModelDiscoveryRequest(provider="openai", credential_name="saved"),
        {"api_key": "os.environ/DISCOVERY_TEST_API_KEY"},
        "openai",
    )
    assert target.api_key.get_secret_value() == "test-environment-value"


def test_manually_entered_key_cannot_read_a_server_environment_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISCOVERY_TEST_API_KEY", "test-environment-value")
    target: Final = resolve_target(
        ModelDiscoveryRequest(
            provider="openai", api_base="https://upstream.example", api_key="os.environ/DISCOVERY_TEST_API_KEY"
        ),
    )
    assert target.api_key.get_secret_value() == "os.environ/DISCOVERY_TEST_API_KEY"


def test_request_provider_cannot_change_a_saved_credentials_native_provider_host():
    request: Final = ModelDiscoveryRequest(provider="openai", credential_name="saved")
    target: Final = resolve_target(request, {"api_key": "saved-secret"}, "anthropic")
    assert httpx.URL(target.url).host == "api.anthropic.com"
    with pytest.raises(HTTPException):
        resolve_target(request, {"api_key": "saved-secret"})


def test_request_rejects_unknown_privilege_fields_and_header_injection():
    with pytest.raises(ValidationError):
        ModelDiscoveryRequest.model_validate({"provider": "openai", "role": "proxy_admin"})
    with pytest.raises(ValidationError):
        ModelDiscoveryRequest(provider="openai", api_key="key\nInjected: header")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "header", "body"),
    (
        ("openai", "authorization", {"data": [{"id": "raw-model"}, {"id": "raw-model"}]}),
        ("anthropic", "x-api-key", {"data": [{"id": "raw-model"}], "has_more": False}),
        ("gemini", "x-goog-api-key", {"models": [{"name": "models/raw-model"}]}),
        ("ollama", "authorization", {"models": [{"name": "raw-model"}]}),
    ),
)
async def test_protocol_authentication_and_raw_ids_never_return_credentials(protocol, header, body):
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers[header] == ("Bearer header-secret" if header == "authorization" else "header-secret")
        return httpx.Response(200, json=body, request=request)

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream))
    target: Final = DiscoveryTarget(
        url="https://upstream.example/custom/models", protocol=protocol, api_key=SecretStr("header-secret")
    )
    try:
        result: Final = await discover_models(target, client)
    finally:
        await client.close()
    assert result.models == ("raw-model",)
    assert result.count == 1
    assert not result.truncated
    assert "header-secret" not in result.model_dump_json()
    assert "api_key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_gemini_pages_only_use_next_page_token_on_the_fixed_endpoint():
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "upstream.example"
        assert request.url.path == "/custom/v1beta/models"
        if request.url.params.get("pageToken") == "opaque-token":
            return httpx.Response(200, json={"models": [{"name": "models/second"}, {"name": "models/first"}]})
        return httpx.Response(200, json={"models": [{"name": "models/first"}], "nextPageToken": "opaque-token"})

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream))
    try:
        result: Final = await discover_models(
            DiscoveryTarget(url="https://upstream.example/custom/v1beta/models", protocol="gemini", api_key=None),
            client,
        )
    finally:
        await client.close()
    assert result.models == ("first", "second")
    assert not result.truncated


@pytest.mark.asyncio
async def test_anthropic_pagination_has_a_hard_page_limit():
    def upstream(request: httpx.Request) -> httpx.Response:
        page: Final = int(request.url.params.get("after_id", "0"))
        return httpx.Response(200, json={"data": [{"id": f"model-{page}"}], "has_more": True, "last_id": str(page + 1)})

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream))
    try:
        result: Final = await discover_models(
            DiscoveryTarget(url="https://upstream.example/v1/models", protocol="anthropic", api_key=None), client
        )
    finally:
        await client.close()
    assert result.count == 5
    assert result.truncated


@pytest.mark.asyncio
async def test_model_limit_reports_truncation_without_extra_pages():
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": f"model-{index}"} for index in range(5002)]})

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream))
    try:
        result: Final = await discover_models(
            DiscoveryTarget(url="https://upstream.example/v1/models", protocol="openai", api_key=None), client
        )
    finally:
        await client.close()
    assert result.count == 5000
    assert result.truncated


@pytest.mark.asyncio
async def test_unrecognized_openai_pagination_is_reported_as_partial_without_following_a_next_url():
    client: Final = AsyncHTTPHandler(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"id": "one-model"}], "has_more": True, "next": "https://other.example/models"}
            ),
        )
    )
    try:
        result: Final = await discover_models(
            DiscoveryTarget(url="https://upstream.example/v1/models", protocol="openai", api_key=None), client
        )
    finally:
        await client.close()
    assert result.count == 1
    assert result.truncated


@pytest.mark.asyncio
async def test_redirects_do_not_forward_credentials_even_when_client_default_follows():
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "upstream.example"
        return httpx.Response(302, headers={"location": "https://untrusted.example/models"})

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream), follow_redirects=True)
    try:
        with pytest.raises(HTTPException) as failure:
            await discover_models(
                DiscoveryTarget(
                    url="https://upstream.example/v1/models", protocol="openai", api_key=SecretStr("private-key")
                ),
                client,
            )
    finally:
        await client.close()
    assert failure.value.status_code == 400
    assert "redirect" in str(failure.value.detail).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fragment"),
    ((401, "401"), (403, "403"), (404, "endpoint"), (429, "rate-limited"), (500, "could not provide")),
)
async def test_upstream_errors_are_distinct_without_echoing_raw_response(status: int, fragment: str):
    client: Final = AsyncHTTPHandler(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text="secret-key-from-upstream-error"))
    )
    try:
        with pytest.raises(HTTPException) as failure:
            await discover_models(
                DiscoveryTarget(url="https://upstream.example/v1/models", protocol="openai", api_key=None), client
            )
    finally:
        await client.close()
    assert fragment in str(failure.value.detail)
    assert "secret-key" not in str(failure.value.detail)


@pytest.mark.asyncio
async def test_empty_model_list_is_a_success_and_not_an_authentication_error():
    client: Final = AsyncHTTPHandler(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    )
    try:
        result: Final = await discover_models(
            DiscoveryTarget(url="https://upstream.example/v1/models", protocol="openai", api_key=None), client
        )
    finally:
        await client.close()
    assert result.models == ()
    assert result.count == 0
    assert not result.truncated


@pytest.mark.asyncio
async def test_network_timeouts_do_not_expose_credentials_or_transport_details():
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-transport-context", request=request)

    client: Final = AsyncHTTPHandler(transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(HTTPException) as failure:
            await discover_models(
                DiscoveryTarget(
                    url="https://upstream.example/v1/models", protocol="openai", api_key=SecretStr("private-key")
                ),
                client,
            )
    finally:
        await client.close()
    assert failure.value.status_code == 504
    assert "private" not in str(failure.value.detail)


@pytest.mark.asyncio
async def test_oversize_discovery_responses_are_bounded_before_json_parsing():
    client: Final = AsyncHTTPHandler(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-length": "3000000"}, content=b"{}")
        )
    )
    try:
        with pytest.raises(HTTPException) as failure:
            await discover_models(
                DiscoveryTarget(url="https://upstream.example/v1/models", protocol="openai", api_key=None), client
            )
    finally:
        await client.close()
    assert failure.value.status_code == 502
    assert "size limit" in str(failure.value.detail)

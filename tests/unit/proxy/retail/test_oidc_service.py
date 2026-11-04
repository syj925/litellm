from collections.abc import Mapping
from typing import Final

import pytest

from litellm.proxy.retail.oidc_config import OidcConfig, OidcFailure
from litellm.proxy.retail.oidc_service import dedicated_redis, session_general_settings, session_settings


def config(changes: Mapping[str, object] | None = None) -> OidcConfig:
    return OidcConfig.model_validate({
        "namespace": "service-qa", "generation": 2, "issuer": "https://identity.test",
        "client_id": "private-client", "client_auth_method": "client_secret_basic",
        "public_origin": "https://gateway.test", "session_seconds": 900,
        "bindings": ({"subject": "private-enabled", "user_id": "existing-user"},
                     {"subject": "private-disabled", "user_id": "former-user", "enabled": False}),
        **(changes or {}),
    })


def test_native_settings_preserve_explicit_disabled_binding_and_server_session_limit() -> None:
    current: Final = config()
    converted: Final = session_settings(current)
    assert converted.session_seconds == current.session_seconds
    assert converted.issuer == current.issuer
    assert converted.generation == current.generation
    assert converted.namespace == current.namespace
    assert tuple((row.subject, row.user_id, row.enabled) for row in converted.bindings) == (
        ("private-enabled", "existing-user", True), ("private-disabled", "former-user", False),
    )
    assert "private-" not in repr(converted)


@pytest.mark.parametrize("changes", (
    {"client_id": "different-client"}, {"client_auth_method": "client_secret_post"},
    {"public_origin": "https://other-gateway.test"}, {"endpoint_origins": ("https://keys.test",)},
))
def test_native_proof_digest_fences_protocol_config_outside_native_settings_fields(
    changes: Mapping[str, object],
) -> None:
    previous: Final = session_settings(config())
    changed: Final = session_settings(config(changes))
    assert previous.bindings == changed.bindings
    assert previous.issuer == changed.issuer
    assert previous.digest != changed.digest


def test_only_ui_header_setting_is_projected_ignoring_non_json_server_callbacks() -> None:
    callback: Final = lambda: "server-only"
    settings: Final = {
        "custom_auth": callback, "opaque_server_object": object(), "litellm_key_header_name": "x-gateway-key",
        "master_key": "private-master", "oidc_subject": "private-subject",
    }
    projected: Final = session_general_settings(settings)
    assert projected == {"litellm_key_header_name": "x-gateway-key"}
    assert settings["custom_auth"] is callback
    assert "private" not in repr(projected)
    assert session_general_settings({"custom_auth": callback}) == {}


@pytest.mark.parametrize("header", (False, 123, "", "bad\nheader", "x" * 129, object()))
def test_invalid_explicit_ui_header_fails_closed_without_coercion(header: object) -> None:
    assert session_general_settings({"litellm_key_header_name": header}) == OidcFailure("invalid")


@pytest.mark.parametrize("query", (
    "max_connections=4000", "socket_timeout=300", "socket_connect_timeout=300", "retry_on_timeout=true",
    "decode_responses=true", "health_check_interval=0", "%6dax_connections=4000", "db=1&db=2",
))
def test_redis_url_cannot_override_auth_pool_resource_or_protocol_limits(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    monkeypatch.setenv("REDIS_URL", f"redis://fixture:private-password@localhost:6379/0?{query}")
    with pytest.raises(ValueError, match="Invalid sign-in coordination configuration") as error:
        dedicated_redis()
    assert "private-password" not in str(error.value)


def test_redis_ssl_and_db_url_options_keep_fixed_pool_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "rediss://fixture:private-password@localhost:6380/0?db=3&ssl_cert_reqs=required")
    client: Final = dedicated_redis()
    pool: Final = client.connection_pool
    assert pool.max_connections == 4
    assert pool.connection_kwargs["socket_timeout"] == pool.connection_kwargs["socket_connect_timeout"] == 1
    assert pool.connection_kwargs["health_check_interval"] == 30
    assert pool.connection_kwargs["decode_responses"] is False
    assert pool.connection_kwargs["db"] == 3
    assert pool.connection_kwargs["ssl_cert_reqs"] == "required"


def test_separate_host_environment_uses_same_fixed_pool_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "4")
    client: Final = dedicated_redis()
    assert client.connection_pool.max_connections == 4
    assert client.connection_pool.connection_kwargs["socket_timeout"] == 1
    assert client.connection_pool.connection_kwargs["db"] == 4

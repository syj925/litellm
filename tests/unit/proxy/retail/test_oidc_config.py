from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.oidc_config import OidcConfig, OidcFailure, load_oidc_config, safe_return_to


def config(changes: Mapping[str, object] | None = None) -> OidcConfig:
    return OidcConfig.model_validate({
        "namespace": "qa", "generation": 1, "issuer": "https://idp.test/tenant",
        "client_id": "fixture-client", "client_auth_method": "client_secret_basic",
        "public_origin": "https://gateway.test", "bindings": [{"subject": "private-subject", "user_id": "owner"}],
        **(changes or {}),
    })


@pytest.mark.parametrize("target", (
    "https://evil.test/ui/", "//evil.test/ui/", "https://gateway.test@evil.test/ui/", "/ui/../admin",
    "/ui/%2e%2e/admin", "/ui/%252e%252e/admin", "/ui/%2f/evil", "/ui\\evil", "/ui/#secret",
    "/ui/login/?redirect_to=https://evil.test", "/ui/logs/?x=%0aInjected", "/ui/?x=%252f", "/admin",
))
def test_return_target_cannot_escape_gateway_ui_or_hide_redirect(target: str) -> None:
    assert safe_return_to(config(), target) == OidcFailure("invalid")


@pytest.mark.parametrize("target,expected", (
    ("/ui", "/ui"), ("/ui/logs/?log_id=request-1", "/ui/logs/?log_id=request-1"),
    ("https://gateway.test/ui/models/", "/ui/models/"),
))
def test_local_or_exact_origin_return_target_is_normalized(target: str, expected: str) -> None:
    assert safe_return_to(config(), target) == expected


@pytest.mark.parametrize("changes", (
    {"issuer": "http://remote.test"}, {"issuer": "https://idp.test/#private"},
    {"issuer": "https://private-subject@idp.test"}, {"public_origin": "https://gateway.test/ui"},
    {"generation": True}, {"session_seconds": 3601}, {"allow_loopback_http": "true"},
    {"allow_loopback_http": True}, {"bindings": [{"subject": "private-subject", "user_id": "a"}] * 2},
))
def test_invalid_private_configuration_hides_input(changes: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        config(changes)
    assert "private-subject" not in str(error.value)


def test_explicit_bindings_digest_and_default_off() -> None:
    current: Final = config()
    assert current.binding("private-subject").user_id == "owner"
    assert current.binding("PRIVATE-subject") is None
    assert current.binding("owner@example.test") is None
    changed: Final = config({"bindings": [{"subject": "private-subject", "user_id": "owner", "enabled": False}]})
    assert changed.binding("private-subject") is None
    assert changed.digest() != current.digest()
    assert load_oidc_config({}) is None
    assert load_oidc_config({"RETAIL_OIDC_SSO_ENABLED": "true"}) == OidcFailure("invalid")

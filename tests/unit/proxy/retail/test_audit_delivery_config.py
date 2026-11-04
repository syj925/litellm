import base64
from collections.abc import Mapping
from dataclasses import asdict
from typing import Final

import pytest

from litellm.proxy.retail.audit_delivery_config import WebhookConfig, WebhookUnavailable, webhook_configuration

_KEY: Final = bytes(range(32))
_ENCODED: Final = "whsec_" + base64.b64encode(_KEY).decode("ascii")


@pytest.mark.parametrize(
    "environment,code",
    (
        ({}, "not_configured"),
        ({"RETAIL_AUDIT_WEBHOOK_URL": "https://receiver.example/events"}, "invalid_configuration"),
        ({"RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED}, "invalid_configuration"),
    ),
)
def test_unconfigured_target_is_a_safe_value(environment: Mapping[str, str], code: str) -> None:
    assert asdict(webhook_configuration(environment)) == {"code": code}


@pytest.mark.parametrize(
    "url",
    (
        "http://receiver.example/events",
        "https://user:private@receiver.example/events",
        "https://receiver.example/events?token=private",
        "https://receiver.example/events#private",
        "https://receiver.example/events?",
        "https://receiver.example/events#",
        "https://receiver.example:bad/events",
        "https://receiver.example:70000/events",
        "https://receiver.example/with space",
        "https://receiver.example/\nprivate",
        "https:///events",
        "file:///private",
        "https://receiver.example/" + "x" * 2048,
    ),
)
def test_unsafe_endpoint_is_not_usable_or_echoed(url: str) -> None:
    result: Final = webhook_configuration({"RETAIL_AUDIT_WEBHOOK_URL": url, "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED})
    assert result == WebhookUnavailable("invalid_configuration")
    assert url not in repr(result)


@pytest.mark.parametrize(
    "secret",
    (
        "raw-keys-are-not-the-standard-format",
        "whsec_not-valid-base64",
        "whsec_" + base64.b64encode(bytes(31)).decode("ascii"),
        "whsec_" + base64.b64encode(bytes(65)).decode("ascii"),
        "whsec_" + "a" * 512,
        "whsec_秘密",
    ),
)
def test_invalid_signing_key_is_not_usable_or_echoed(secret: str) -> None:
    result: Final = webhook_configuration(
        {"RETAIL_AUDIT_WEBHOOK_URL": "https://receiver.example/events", "RETAIL_AUDIT_WEBHOOK_SECRET": secret}
    )
    assert result == WebhookUnavailable("invalid_configuration")
    assert secret not in repr(result)


def test_valid_config_decodes_key_and_does_not_reveal_credentials() -> None:
    result: Final = webhook_configuration(
        {"RETAIL_AUDIT_WEBHOOK_URL": "https://Receiver.Example/events", "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED}
    )
    assert isinstance(result, WebhookConfig)
    assert result.url == "https://receiver.example/events"
    assert result.secret == _KEY
    assert repr(result) == "WebhookConfig()"


def test_endpoint_and_signing_key_both_select_the_frozen_configuration() -> None:
    first: Final = webhook_configuration(
        {"RETAIL_AUDIT_WEBHOOK_URL": "https://receiver.example/events", "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED}
    )
    canonical: Final = webhook_configuration(
        {"RETAIL_AUDIT_WEBHOOK_URL": "https://RECEIVER.example/events", "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED}
    )
    changed_url: Final = webhook_configuration(
        {"RETAIL_AUDIT_WEBHOOK_URL": "https://other.example/events", "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED}
    )
    changed_key: Final = webhook_configuration(
        {
            "RETAIL_AUDIT_WEBHOOK_URL": "https://receiver.example/events",
            "RETAIL_AUDIT_WEBHOOK_SECRET": "whsec_" + base64.b64encode(bytes(32)).decode("ascii"),
        }
    )
    assert isinstance(first, WebhookConfig) and isinstance(canonical, WebhookConfig)
    assert isinstance(changed_url, WebhookConfig) and isinstance(changed_key, WebhookConfig)
    assert first.fingerprint == canonical.fingerprint
    assert first.fingerprint != changed_url.fingerprint
    assert first.fingerprint != changed_key.fingerprint


def test_local_http_requires_an_explicit_operator_override() -> None:
    result: Final = webhook_configuration(
        {
            "RETAIL_AUDIT_WEBHOOK_URL": "http://127.0.0.1:9090/events",
            "RETAIL_AUDIT_WEBHOOK_SECRET": _ENCODED,
            "RETAIL_AUDIT_WEBHOOK_ALLOW_HTTP": "true",
        }
    )
    assert isinstance(result, WebhookConfig)
    assert result.url == "http://127.0.0.1:9090/events"

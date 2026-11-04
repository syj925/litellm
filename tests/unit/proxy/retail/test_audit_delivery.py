from typing import Final

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.audit_delivery import (
    ConfigureAuditDelivery,
    DiscardAuditDeliveries,
    RetryAuditDelivery,
    delivery_status_view,
)
from litellm.proxy.retail.audit_delivery_config import WebhookConfig, WebhookUnavailable
from litellm.proxy.retail.audit_delivery_store import DeliveryCounts, DeliveryStatus

_STATUS: Final = DeliveryStatus(
    enabled=False,
    config_version=2,
    configuration_matches=False,
    activated_at=None,
    retained_count=0,
    overflow_count=0,
    last_overflow_at=None,
    counts=DeliveryCounts(pending=0, processing=0, delivered=0, failed=0, discarded=0),
    last_delivered_at=None,
    last_failure_at=None,
)


@pytest.mark.parametrize("version", (True, "2", 0, -1, 9_223_372_036_854_775_807))
def test_writes_require_a_positive_exact_version(version: object) -> None:
    with pytest.raises(ValidationError):
        RetryAuditDelivery.model_validate({"expected_version": version})


@pytest.mark.parametrize("enabled", ("true", "false", 0, 1, None))
def test_enforcement_switch_does_not_coerce_unknown_input(enabled: object) -> None:
    with pytest.raises(ValidationError):
        ConfigureAuditDelivery.model_validate({"expected_version": 1, "enabled": enabled})


@pytest.mark.parametrize("field", ("url", "secret", "target", "config_fingerprint"))
def test_admin_api_cannot_take_an_arbitrary_destination(field: str) -> None:
    with pytest.raises(ValidationError):
        ConfigureAuditDelivery.model_validate({"expected_version": 1, "enabled": True, field: "private-input"})


@pytest.mark.parametrize("confirmation", (None, True, "yes", "delete", "discard"))
def test_discard_requires_the_explicit_pending_delivery_confirmation(confirmation: object) -> None:
    with pytest.raises(ValidationError):
        DiscardAuditDeliveries.model_validate({"expected_version": 1, "confirmation": confirmation})


def test_status_explains_missing_configuration_without_exposing_destination_or_key() -> None:
    config: Final = WebhookConfig("https://private-receiver.example/marker", b"private-key-marker", "private-fingerprint")
    visible: Final = delivery_status_view(_STATUS, config)
    assert visible.destination_configured and visible.configuration_error is None
    assert "private" not in visible.model_dump_json()
    assert delivery_status_view(_STATUS, WebhookUnavailable("not_configured")).configuration_error == "not_configured"
    assert not delivery_status_view(_STATUS, WebhookUnavailable("invalid_configuration")).destination_configured

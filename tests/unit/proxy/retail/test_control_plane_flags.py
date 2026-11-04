import pytest

from litellm.proxy.retail.control_plane_flags import (
    audit_delivery_enabled,
    model_budgets_enabled,
    native_audit_enabled,
    object_metadata_enabled,
    operation_audit_enabled,
    organization_delegation_enabled,
    organization_finance_delegation_enabled,
    organization_finance_enabled,
    organizations_enabled,
    report_export_enabled,
)


@pytest.mark.parametrize("enabled", (False, True))
def test_audit_rollback_disables_dependent_management_writes(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", str(enabled).lower())
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_OBJECT_METADATA_ENABLED", "true")
    monkeypatch.setenv("RETAIL_MODEL_BUDGETS_ENABLED", "true")
    monkeypatch.setenv("RETAIL_AUDIT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    assert (
        operation_audit_enabled(),
        organizations_enabled(),
        organization_delegation_enabled(),
        organization_finance_enabled(),
        organization_finance_delegation_enabled(),
        report_export_enabled(),
        object_metadata_enabled(),
        model_budgets_enabled(),
        audit_delivery_enabled(),
        native_audit_enabled(),
    ) == (enabled, enabled, enabled, enabled, enabled, enabled, enabled, enabled, enabled, enabled)


def test_organization_rollback_preserves_personal_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORGANIZATIONS_ENABLED", "false")
    monkeypatch.setenv("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("RETAIL_ORG_FINANCE_ENABLED", "true")
    monkeypatch.setenv("RETAIL_REPORT_EXPORT_ENABLED", "true")
    assert (organizations_enabled(), organization_delegation_enabled(), organization_finance_enabled(),
            report_export_enabled()) == (False, False, False, True)


def test_unconfigured_modules_stay_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RETAIL_OPERATION_AUDIT_ENABLED",
        "RETAIL_ORGANIZATIONS_ENABLED",
        "RETAIL_ORGANIZATION_DELEGATION_ENABLED",
        "RETAIL_ORG_FINANCE_ENABLED",
        "RETAIL_ORG_FINANCE_DELEGATION_ENABLED",
        "RETAIL_REPORT_EXPORT_ENABLED",
        "RETAIL_OBJECT_METADATA_ENABLED",
        "RETAIL_MODEL_BUDGETS_ENABLED",
        "RETAIL_AUDIT_DELIVERY_ENABLED",
        "RETAIL_NATIVE_AUDIT_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert (
        operation_audit_enabled(),
        organizations_enabled(),
        organization_delegation_enabled(),
        organization_finance_enabled(),
        organization_finance_delegation_enabled(),
        report_export_enabled(),
        object_metadata_enabled(),
        model_budgets_enabled(),
        audit_delivery_enabled(),
        native_audit_enabled(),
    ) == (False, False, False, False, False, False, False, False, False, False)


@pytest.mark.parametrize(
    "disabled",
    ("RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED", "RETAIL_ORGANIZATION_DELEGATION_ENABLED",
     "RETAIL_ORG_FINANCE_ENABLED", "RETAIL_ORG_FINANCE_DELEGATION_ENABLED"),
)
def test_finance_delegation_cannot_outlive_any_authority_dependency(
    monkeypatch: pytest.MonkeyPatch, disabled: str,
) -> None:
    for name in (
        "RETAIL_OPERATION_AUDIT_ENABLED", "RETAIL_ORGANIZATIONS_ENABLED", "RETAIL_ORGANIZATION_DELEGATION_ENABLED",
        "RETAIL_ORG_FINANCE_ENABLED", "RETAIL_ORG_FINANCE_DELEGATION_ENABLED",
    ):
        monkeypatch.setenv(name, "true")
    assert organization_finance_delegation_enabled() is True
    monkeypatch.setenv(disabled, "false")
    assert organization_finance_delegation_enabled() is False

import os


def operation_audit_enabled() -> bool:
    return os.environ.get("RETAIL_OPERATION_AUDIT_ENABLED", "false").lower() == "true"


def organizations_enabled() -> bool:
    return operation_audit_enabled() and os.environ.get("RETAIL_ORGANIZATIONS_ENABLED", "false").lower() == "true"


def organization_delegation_enabled() -> bool:
    return (
        organizations_enabled() and os.environ.get("RETAIL_ORGANIZATION_DELEGATION_ENABLED", "false").lower() == "true"
    )


def report_export_enabled() -> bool:
    return operation_audit_enabled() and os.environ.get("RETAIL_REPORT_EXPORT_ENABLED", "false").lower() == "true"

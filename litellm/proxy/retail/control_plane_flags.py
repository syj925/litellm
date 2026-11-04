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


def object_metadata_enabled() -> bool:
    return operation_audit_enabled() and os.environ.get("RETAIL_OBJECT_METADATA_ENABLED", "false").lower() == "true"


def model_budgets_enabled() -> bool:
    return operation_audit_enabled() and os.environ.get("RETAIL_MODEL_BUDGETS_ENABLED", "false").lower() == "true"


def audit_delivery_enabled() -> bool:
    return operation_audit_enabled() and os.environ.get("RETAIL_AUDIT_DELIVERY_ENABLED", "false").lower() == "true"

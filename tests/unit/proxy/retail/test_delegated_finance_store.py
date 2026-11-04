from typing import Final

import pytest

from litellm.proxy.retail.delegated_finance_store import _ConsumptionRow, consumption_item


@pytest.mark.parametrize("charge,expected", ((0, "0.000000000"), (-1, "0.000000001"),
                                             (-123456789, "0.123456789"), (-9000000000000000000, "9000000000.000000000")))
def test_delegated_projection_keeps_exact_customer_charge_without_identifiers(charge: int, expected: str) -> None:
    row: Final = _ConsumptionRow.model_validate({
        "sequence": 1, "model": "public-model", "captured_at": "2025-01-01T00:00:00Z", "state": "settled",
        "charge_nanos": charge, "user_id": "private-fixture-account", "request_id": "private-fixture-request",
        "assignment_version": 12, "membership_version": 22, "organization_version": 31,
    })
    result: Final = consumption_item(row).model_dump(mode="json")
    assert set(result) == {"sequence", "model", "captured_at", "state", "settled_usd"}
    assert result["settled_usd"] == expected
    assert "private-fixture" not in str(result)


@pytest.mark.parametrize("state", ("pending", "unknown", "conflict", "released"))
def test_unsettled_delegated_rows_show_missing_charge_instead_of_zero(state: str) -> None:
    row: Final = _ConsumptionRow.model_validate({
        "sequence": 3, "model": "public-model", "captured_at": "2025-01-01T00:00:00Z", "state": state,
        "charge_nanos": None,
    })
    assert consumption_item(row).settled_usd is None

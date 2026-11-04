from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from litellm.proxy.retail.budget_accounting import BudgetPolicyRow


def _row(spent: Decimal, held: Decimal) -> BudgetPolicyRow:
    now: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return BudgetPolicyRow.model_validate(
        {
            "id": uuid4(),
            "user_id": "account",
            "model": "public-model",
            "effective_from": now,
            "ends_at": now + timedelta(days=1),
            "limit_nanos": 1,
            "spent_nanos": spent,
            "held_nanos": held,
            "enabled": True,
            "config_version": 1,
            "accounting_version": 5,
            "source": "management_api",
            "created_at": now,
            "created_by": "admin",
            "updated_at": now,
            "updated_by": "admin",
            "accounted_at": now,
        }
    )


def test_numeric_budget_counters_keep_integer_precision_above_signed_bigint() -> None:
    spent: Final = Decimal(900000000000000000000)
    held: Final = Decimal(100000000000000000001)
    row: Final = _row(spent, held)
    assert row.spent_nanos == 900000000000000000000 and row.held_nanos == 100000000000000000001
    assert row.config_version == 1 and row.accounting_version == 5


@pytest.mark.parametrize("invalid", (Decimal("0.5"), Decimal("NaN"), Decimal("Infinity")))
def test_budget_counter_projection_rejects_fractional_or_nonfinite_database_values(invalid: Decimal) -> None:
    with pytest.raises(ValidationError):
        _row(invalid, Decimal(0))

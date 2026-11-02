from typing import Final

import pytest

from litellm.proxy.retail.admission import RequestAdmission


class _Pressure:
    def __init__(self) -> None:
        self.overloaded = False

    def __call__(self) -> bool:
        return self.overloaded


def test_capacity_and_logger_pressure_reject_new_work_until_resources_are_released() -> None:
    pressure: Final = _Pressure()
    admission: Final = RequestAdmission(2, pressure)
    assert admission.try_acquire() and admission.try_acquire()
    assert not admission.try_acquire()
    admission.release()
    pressure.overloaded = True
    assert not admission.try_acquire()
    assert admission.stats.active == 1 and admission.stats.rejected == 2
    pressure.overloaded = False
    assert admission.try_acquire()
    admission.release()
    admission.release()
    assert admission.stats.active == 0 and admission.try_acquire()


def test_financial_connection_pressure_stops_new_requests_and_recovers_without_database_queries() -> None:
    pressure: Final = _Pressure()
    admission: Final = RequestAdmission(2, lambda: False, lambda threshold: pressure.overloaded and threshold > 0)
    assert admission.try_acquire()
    pressure.overloaded = True
    assert not admission.try_acquire() and admission.stats.active == 1
    admission.release()
    pressure.overloaded = False
    assert admission.try_acquire() and admission.stats.rejected == 1


@pytest.mark.parametrize("limit", (0, -1, 100001))
def test_invalid_resource_budget_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        RequestAdmission(limit, _Pressure())

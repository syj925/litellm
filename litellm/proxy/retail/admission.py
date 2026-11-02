import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Final


def _logging_pressure() -> bool:
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    from .settlement_worker import settlement_is_overloaded

    return getattr(GLOBAL_LOGGING_WORKER, "is_overloaded", False) is True or settlement_is_overloaded()


def _finance_pressure(threshold: int) -> bool:
    from .store import get_store

    if not get_store.cache_info().currsize:
        return False
    return get_store().pool_stats()["finance"].get("requests_waiting", 0) >= threshold


@dataclass(frozen=True, slots=True)
class AdmissionStats:
    active: int
    limit: int
    rejected: int


class RequestAdmission:
    def __init__(
        self, limit: int | None = None, pressure: Callable[[], bool] = _logging_pressure,
        finance_pressure: Callable[[int], bool] = _finance_pressure,
    ) -> None:
        selected: Final = int(os.getenv("RETAIL_MAX_INFLIGHT", "512")) if limit is None else limit
        if not 1 <= selected <= 100_000:
            raise ValueError("Retail in-flight limit must be between 1 and 100000")
        self._limit = selected
        self._pressure = pressure
        self._finance_pressure = finance_pressure
        self._waiting_threshold = int(os.getenv("RETAIL_DB_ADMISSION_WAITING", "16"))
        if not 1 <= self._waiting_threshold <= 4096:
            raise ValueError("Retail admission waiting threshold must be between 1 and 4096")
        self._active = 0
        self._rejected = 0

    @property
    def stats(self) -> AdmissionStats:
        return AdmissionStats(self._active, self._limit, self._rejected)

    def try_acquire(self) -> bool:
        if self._active >= self._limit or self._pressure() or self._finance_pressure(self._waiting_threshold):
            self._rejected += 1
            return False
        self._active += 1
        return True

    def release(self) -> None:
        if self._active:
            self._active -= 1


@lru_cache(maxsize=1)
def get_request_admission() -> RequestAdmission:
    return RequestAdmission()

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import operation_audit_enabled
from .coordination_guard import close_coordination_guard, start_coordination_guard
from .model_budgets import router as model_budgets_router
from .object_metadata import router as object_metadata_router
from .organizations import router as organizations_router
from .report_worker import close_report_worker, start_report_worker
from .reports import router as reports_router
from .store import get_store


async def _initialize_management() -> None:
    while operation_audit_enabled():
        try:
            store: Final = get_store()
            await store.initialize()
            await store.initialize_control_plane()
            await start_report_worker(store)
        except Exception as exc:  # noqa: BLE001  # optional management outages must not stop inference or settlement
            verbose_proxy_logger.warning("Retail management startup deferred (%s)", type(exc).__name__)
        else:
            return
        await asyncio.sleep(5)


@asynccontextmanager
async def control_plane_lifespan(app: FastAPI) -> AsyncIterator[None]:
    await start_coordination_guard()
    task: Final = asyncio.create_task(_initialize_management(), name="retail-management-startup")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            await close_report_worker()
        finally:
            await close_coordination_guard()


router: Final = APIRouter(lifespan=control_plane_lifespan)
router.include_router(organizations_router)
router.include_router(reports_router)
router.include_router(object_metadata_router)
router.include_router(model_budgets_router)

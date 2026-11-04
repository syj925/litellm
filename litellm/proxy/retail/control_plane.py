import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI

from litellm._logging import verbose_proxy_logger

from .audit_delivery import router as audit_delivery_router
from .audit_delivery_worker import close_audit_delivery_worker, start_audit_delivery_worker
from .control_plane_flags import native_audit_enabled, operation_audit_enabled
from .coordination_guard import close_coordination_guard, start_coordination_guard
from .custom_secret_adapter import close_managed_secrets, start_managed_secrets
from .delegated_finance import router as delegated_finance_router
from .model_budgets import router as model_budgets_router
from .monthly_budget_worker import close_monthly_budget_worker, start_monthly_budget_worker
from .monthly_budgets import router as monthly_budgets_router
from .native_audit_worker import close_native_audit_worker, start_native_audit_worker
from .object_metadata import router as object_metadata_router
from .oidc import router as oidc_router
from .oidc_http import install_oidc_access_filter
from .oidc_service import close_oidc_service, start_oidc_service
from .organization_finance import router as organization_finance_router
from .organization_reports import router as organization_reports_router
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
            await start_audit_delivery_worker(store)
            await start_monthly_budget_worker(store)
            if native_audit_enabled():
                from litellm.proxy import proxy_server

                if proxy_server.prisma_client is None:
                    raise RuntimeError("Native audit storage is unavailable")
                await start_native_audit_worker(proxy_server.prisma_client, store)
        except Exception as exc:  # noqa: BLE001  # optional management outages must not stop inference or settlement
            verbose_proxy_logger.warning("Retail management startup deferred (%s)", type(exc).__name__)
        else:
            return
        await asyncio.sleep(5)


@asynccontextmanager
async def control_plane_lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncExitStack() as cleanup:
        install_oidc_access_filter()
        await start_managed_secrets()
        cleanup.push_async_callback(close_managed_secrets)
        await start_coordination_guard()
        cleanup.push_async_callback(close_coordination_guard)
        await start_oidc_service()
        cleanup.push_async_callback(close_oidc_service)
        cleanup.push_async_callback(close_audit_delivery_worker)
        cleanup.push_async_callback(close_report_worker)
        cleanup.push_async_callback(close_native_audit_worker)
        cleanup.push_async_callback(close_monthly_budget_worker)
        task: Final = asyncio.create_task(_initialize_management(), name="retail-management-startup")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


router: Final = APIRouter(lifespan=control_plane_lifespan)
router.include_router(organizations_router)
router.include_router(reports_router)
router.include_router(object_metadata_router)
router.include_router(model_budgets_router)
router.include_router(monthly_budgets_router)
router.include_router(audit_delivery_router)
router.include_router(organization_finance_router)
router.include_router(organization_reports_router)
router.include_router(delegated_finance_router)
router.include_router(oidc_router)

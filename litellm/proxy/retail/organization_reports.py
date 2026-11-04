from collections.abc import Awaitable
from typing import Final, TypeVar
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from .api import Admin, OperationKey
from .control_plane_access import verify_actor
from .control_plane_flags import organization_finance_enabled, report_export_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .organization_finance_store import organization_identifier
from .organization_report_store import (
    OrganizationReportJob,
    OrganizationReportPage,
    OrganizationReportStore,
    OrganizationReportWindow,
)
from .organization_store import require_admin
from .report_store import ReportError, get_report_store
from .reports import download_range

_Result = TypeVar("_Result")  # rebind-ok: type variables require an unannotated typing declaration
_NO_STORE: Final = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Vary": "Authorization, Cookie"}
_ERROR_MESSAGES: Final = {
    "window_too_large": "The organization report window is too large. Select a shorter interval.",
    "future_window": "The report cannot start in the future.",
    "row_limit_exceeded": "The organization report contains too many requests. Select a shorter interval.",
    "row_projection_too_large": "A request identifier exceeds the safe export limit. Contact an administrator.",
    "artifact_too_large": "The export is too large. Select a shorter interval.",
    "report_busy": "Report generation is busy. Retry later.",
    "report_quota_reached": "The daily report limit has been reached.",
    "organization_not_found": "The requested organization is unavailable.",
    "report_not_found": "Report not found.",
    "idempotency_conflict": "This idempotency key was already used for different report filters.",
    "report_expired": "This report has expired. Create a new report.",
    "report_not_ready": "This report is not ready for download.",
    "snapshot_inconsistent": "Organization consumption cannot currently be verified.",
}


def require_organization_reports() -> None:
    if not organization_finance_enabled() or not report_export_enabled():
        raise HTTPException(404, "Not found", headers=_NO_STORE)


def _queries(request: Request, allowed: frozenset[str] = frozenset()) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Unsupported organization report query fields", headers=_NO_STORE)


def _organization_id(value: str) -> str:
    try:
        return organization_identifier(value)
    except ValueError as exc:
        raise HTTPException(422, "Enter a valid organization identifier", headers=_NO_STORE) from exc


async def _execute(action: Awaitable[_Result]) -> _Result:
    try:
        return await action
    except ReportError as exc:
        raise HTTPException(
            exc.status, _ERROR_MESSAGES.get(exc.code, "Report storage is temporarily unavailable."), headers=_NO_STORE
        ) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Report storage is temporarily unavailable", headers=_NO_STORE) from exc


router: Final = APIRouter(
    prefix="/retail/admin/organizations/{organization_id}/reports", tags=["Retail organization reports"],
    dependencies=[Depends(require_organization_reports)], route_class=PrivateManagementRoute,
)


@router.post("", response_model=OrganizationReportJob, status_code=202)
async def create_organization_report(
    request: Request, organization_id: str, data: OrganizationReportWindow, user: Admin,
    store: Store, operation_key: OperationKey,
) -> OrganizationReportJob:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request)
    storage: Final = OrganizationReportStore(get_report_store(store))
    return await _execute(storage.create(actor.user_id, _organization_id(organization_id), operation_key, data))


@router.get("", response_model=OrganizationReportPage)
async def list_organization_reports(
    request: Request, organization_id: str, user: Admin, store: Store,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
) -> OrganizationReportPage:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request, frozenset(("limit", "offset")))
    storage: Final = OrganizationReportStore(get_report_store(store))
    return await _execute(storage.list(actor.user_id, _organization_id(organization_id), limit, offset))


@router.get("/{report_id}", response_model=OrganizationReportJob)
async def get_organization_report(
    request: Request, organization_id: str, report_id: UUID, user: Admin, store: Store,
) -> OrganizationReportJob:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request)
    storage: Final = OrganizationReportStore(get_report_store(store))
    job: Final = await _execute(storage.get(report_id, actor.user_id, _organization_id(organization_id)))
    return job.public()


@router.get("/{report_id}/download", response_class=Response)
async def download_organization_report(
    request: Request, organization_id: str, report_id: UUID, user: Admin, store: Store,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request)
    storage: Final = OrganizationReportStore(get_report_store(store))
    job: Final = await _execute(storage.get(report_id, actor.user_id, _organization_id(organization_id)))
    interval: Final = download_range(range_header, job.artifact_bytes) if job.state == "ready" else None
    content: Final = await _execute(storage.artifact(job, interval))
    headers: Final = {
        **_NO_STORE, "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="retail-organization-{report_id}.csv"',
        "Content-Length": str(len(content)), "ETag": f'"{job.artifact_sha256}"',
        **({"Content-Range": f"bytes {interval[0]}-{interval[1]}/{job.artifact_bytes}"} if interval else {}),
    }
    return Response(
        content=content, status_code=206 if interval else 200, media_type="text/csv; charset=utf-8", headers=headers,
    )

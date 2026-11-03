from typing import Final
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from pydantic import Field

from .api import OperationKey, User
from .control_plane_access import active_target_users, verify_actor, verify_target_user
from .control_plane_flags import report_export_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .report_store import (
    ReportError,
    ReportJob,
    ReportPage,
    ReportWindow,
    byte_range,
    get_report_store,
)

_ERROR_MESSAGES: Final = {
    "window_too_large": "The report window is too large. Select a shorter interval.",
    "future_window": "The report cannot start in the future.",
    "row_limit_exceeded": "The report contains too many ledger rows. Select a shorter interval.",
    "row_projection_too_large": "A ledger identifier exceeds the safe export limit. Contact an administrator.",
    "artifact_too_large": "The export is too large. Select a shorter interval.",
    "report_busy": "Report generation is busy. Retry later.",
    "report_quota_reached": "The daily report limit has been reached.",
    "account_not_found": "Report account not found.",
    "report_not_found": "Report not found.",
    "idempotency_conflict": "This idempotency key was already used for different report filters.",
    "report_expired": "This report has expired. Create a new report.",
    "report_not_ready": "This report is not ready for download.",
    "invalid_range": "The requested download range is not available.",
}
_NO_STORE: Final = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Vary": "Authorization, Cookie"}


def require_reports_enabled() -> None:
    if not report_export_enabled():
        raise HTTPException(404, "Not found", headers=_NO_STORE)


router: Final = APIRouter(
    prefix="/retail",
    tags=["Retail ledger reports"],
    dependencies=[Depends(require_reports_enabled)],
    route_class=PrivateManagementRoute,
)


class CreateReportRequest(ReportWindow):
    account_id: str | None = Field(default=None, min_length=1, max_length=191)


def report_exception(error: ReportError) -> HTTPException:
    return HTTPException(
        error.status, _ERROR_MESSAGES.get(error.code, "Report storage is temporarily unavailable."), headers=_NO_STORE
    )


def download_range(value: str | None, length: int) -> tuple[int, int] | None:
    try:
        return byte_range(value, length)
    except ReportError as exc:
        raise HTTPException(
            416, _ERROR_MESSAGES["invalid_range"], headers={**_NO_STORE, "Content-Range": f"bytes */{length}"}
        ) from exc


@router.post("/reports", response_model=ReportJob, status_code=202)
async def create_report(
    data: CreateReportRequest,
    user: User,
    store: Store,
    operation_key: OperationKey,
    response: Response,
) -> ReportJob:
    actor: Final = await verify_actor(user, store)
    if not actor.can_write:
        raise HTTPException(403, "Read-only accounts cannot create export jobs", headers=_NO_STORE)
    account_id: Final = data.account_id or actor.user_id
    if account_id != actor.user_id:
        if not actor.is_admin:
            raise HTTPException(403, "Reports for another account require administrator permission", headers=_NO_STORE)
        await verify_target_user(account_id)
    window: Final = ReportWindow.model_validate(data.model_dump(exclude={"account_id"}))
    try:
        job: Final = await get_report_store(store).create(actor.user_id, account_id, operation_key, window)
    except ReportError as exc:
        raise report_exception(exc) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Report storage is temporarily unavailable", headers=_NO_STORE) from exc
    response.headers.update(_NO_STORE)
    return job


@router.get("/reports", response_model=ReportPage)
async def list_reports(
    user: User,
    store: Store,
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> ReportPage:
    actor: Final = await verify_actor(user, store)
    try:
        storage: Final = get_report_store(store)
        targets: Final = await storage.target_accounts(actor.user_id) if actor.is_admin else ()
        active: Final = await active_target_users(targets) if targets else frozenset()
        page: Final = await storage.list(
            actor.user_id, actor.is_admin, limit, offset, allowed_accounts=(actor.user_id, *active)
        )
    except ReportError as exc:
        raise report_exception(exc) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Report storage is temporarily unavailable", headers=_NO_STORE) from exc
    response.headers.update(_NO_STORE)
    return page


@router.get("/reports/{report_id}", response_model=ReportJob)
async def get_report(report_id: UUID, user: User, store: Store, response: Response) -> ReportJob:
    actor: Final = await verify_actor(user, store)
    try:
        job: Final = await get_report_store(store).get(report_id, actor.user_id, actor.is_admin)
    except ReportError as exc:
        raise report_exception(exc) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Report storage is temporarily unavailable", headers=_NO_STORE) from exc
    if job.account_id != actor.user_id:
        await verify_target_user(job.account_id)
    response.headers.update(_NO_STORE)
    return job.public()


@router.get("/reports/{report_id}/download", response_class=Response)
async def download_report(
    report_id: UUID,
    user: User,
    store: Store,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    actor: Final = await verify_actor(user, store)
    try:
        storage: Final = get_report_store(store)
        job: Final = await storage.get(report_id, actor.user_id, actor.is_admin)
        if job.account_id != actor.user_id:
            await verify_target_user(job.account_id)
        interval: Final = download_range(range_header, job.artifact_bytes) if job.state == "ready" else None
        content: Final = await storage.artifact(job, interval)
    except ReportError as exc:
        raise report_exception(exc) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Report storage is temporarily unavailable", headers=_NO_STORE) from exc
    headers: Final = {
        **_NO_STORE,
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="retail-ledger-{report_id}.csv"',
        "Content-Length": str(len(content)),
        "ETag": f'"{job.artifact_sha256}"',
        **({"Content-Range": f"bytes {interval[0]}-{interval[1]}/{job.artifact_bytes}"} if interval else {}),
    }
    return Response(
        content=content, status_code=206 if interval else 200, media_type="text/csv; charset=utf-8", headers=headers
    )

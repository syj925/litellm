from collections.abc import Awaitable
from datetime import datetime
from typing import Final, TypeVar
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from .api import Admin, OperationKey, User
from .budget_management import validate_user_id
from .control_plane_access import ControlPlaneActor, verify_actor, verify_target_user
from .control_plane_flags import organization_finance_delegation_enabled, report_export_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .delegated_finance_store import DelegatedConsumption, DelegatedFinanceStore
from .delegated_report_store import DelegatedReportJob, DelegatedReportPage, DelegatedReportStore
from .finance_grants import FinanceAccess, FinanceGrantStore, FinanceGrantUpdate, FinanceGrantView, FinanceScopePage
from .models import IdempotencyConflict, WalletError
from .organization_finance_store import organization_identifier
from .organization_report_store import OrganizationReportWindow
from .organization_store import require_admin
from .report_store import ReportError, get_report_store
from .reports import download_range

_Result = TypeVar("_Result")  # rebind-ok: type variables require an unannotated typing declaration
_NO_STORE: Final = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Vary": "Authorization, Cookie"}
_PORTAL_FINANCE_ROLES: Final = frozenset((
    "proxy_admin", "proxy_admin_viewer", "internal_user", "internal_user_viewer", "customer",
))
_REPORT_MESSAGES: Final = {
    "window_too_large": "The report window is too large. Select a shorter interval.",
    "future_window": "The report cannot start in the future.",
    "row_limit_exceeded": "The report contains too many requests. Select a shorter interval.",
    "row_projection_too_large": "The report cannot safely represent this request. Contact an administrator.",
    "artifact_too_large": "The export is too large. Select a shorter interval.",
    "report_busy": "Report generation is busy. Retry later.",
    "report_quota_reached": "The daily report limit has been reached.",
    "organization_not_found": "The requested organization is unavailable.",
    "report_not_found": "Report not found.",
    "idempotency_conflict": "This idempotency key was already used for different report filters or access.",
    "report_expired": "This report has expired. Create a new report.",
    "report_not_ready": "This report is not ready for download.",
    "snapshot_inconsistent": "Organization consumption cannot currently be verified.",
}


def _enabled() -> None:
    if not organization_finance_delegation_enabled():
        raise HTTPException(404, "Not found", headers=_NO_STORE)


def _reports_enabled() -> None:
    if not report_export_enabled():
        raise HTTPException(404, "Not found", headers=_NO_STORE)


def _queries(request: Request, allowed: frozenset[str] = frozenset()) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Unsupported finance query fields", headers=_NO_STORE)


def _organization_id(value: str) -> str:
    try:
        return organization_identifier(value)
    except ValueError as exc:
        raise HTTPException(422, "Enter a valid organization identifier", headers=_NO_STORE) from exc


def scope_ids(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if len(value) > 5000:
        raise HTTPException(422, "Too many organization identifiers", headers=_NO_STORE)
    identifiers: Final = tuple(dict.fromkeys(value.split(",")))
    if len(identifiers) > 25 or any(any(char in item for char in "*?%") for item in identifiers):
        raise HTTPException(422, "Select at most 25 exact organization identifiers", headers=_NO_STORE)
    return tuple(_organization_id(item) for item in identifiers)


async def _actor(user: User, store: Store) -> ControlPlaneActor:
    actor: Final = await verify_actor(user, store)
    if actor.native_role not in _PORTAL_FINANCE_ROLES:
        raise HTTPException(403, "This account cannot access delegated finance", headers=_NO_STORE)
    if not actor.can_write:
        raise HTTPException(403, "This account cannot access delegated finance", headers=_NO_STORE)
    return actor


async def _execute(action: Awaitable[_Result]) -> _Result:
    try:
        return await action
    except ReportError as exc:
        raise HTTPException(
            exc.status, _REPORT_MESSAGES.get(exc.code, "Finance storage is temporarily unavailable."), headers=_NO_STORE,
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, "The idempotency key was already used for another operation", headers=_NO_STORE) from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests, WalletError) as exc:
        raise HTTPException(503, "Finance storage is temporarily unavailable", headers=_NO_STORE) from exc


router: Final = APIRouter(
    prefix="/retail", tags=["Delegated organization finance"], dependencies=[Depends(_enabled)],
    route_class=PrivateManagementRoute,
)
_REPORTS: Final = [Depends(_reports_enabled)]


@router.get("/admin/organizations/{organization_id}/finance-grants/{user_id}", response_model=FinanceGrantView)
async def get_finance_grant(
    request: Request, organization_id: str, user_id: str, user: Admin, store: Store,
) -> FinanceGrantView:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request)
    validate_user_id(user_id)
    return await _execute(FinanceGrantStore(store).get(_organization_id(organization_id), user_id, actor))


@router.patch("/admin/organizations/{organization_id}/finance-grants/{user_id}", response_model=FinanceGrantView)
async def update_finance_grant(
    request: Request, organization_id: str, user_id: str, data: FinanceGrantUpdate,
    user: Admin, store: Store, key: OperationKey,
) -> FinanceGrantView:
    actor: Final = await verify_actor(user, store)
    require_admin(actor)
    _queries(request)
    validate_user_id(user_id)
    selected: Final = _organization_id(organization_id)
    if data.can_view or data.can_export:
        await verify_target_user(user_id, writable=True, allowed_roles=_PORTAL_FINANCE_ROLES)
    return await _execute(FinanceGrantStore(store).update(selected, user_id, data, actor, key))


@router.get("/organization-finance/organizations", response_model=FinanceScopePage)
async def finance_organizations(
    request: Request, user: User, store: Store, q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
    ids: str | None = Query(default=None, max_length=5000),
) -> FinanceScopePage:
    actor: Final = await _actor(user, store)
    _queries(request, frozenset(("q", "limit", "offset", "ids")))
    return await _execute(FinanceGrantStore(store).scopes(actor.user_id, q=q, limit=limit, offset=offset, ids=scope_ids(ids)))


@router.get("/organizations/{organization_id}/finance/access", response_model=FinanceAccess)
async def finance_access(request: Request, organization_id: str, user: User, store: Store) -> FinanceAccess:
    actor: Final = await _actor(user, store)
    _queries(request)
    return await _execute(FinanceGrantStore(store).access(_organization_id(organization_id), actor.user_id))


@router.get("/organizations/{organization_id}/finance/consumption", response_model=DelegatedConsumption)
async def finance_consumption(
    request: Request, organization_id: str, user: User, store: Store,
    start: datetime | None = None, end: datetime | None = None,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
) -> DelegatedConsumption:
    actor: Final = await _actor(user, store)
    _queries(request, frozenset(("start", "end", "limit", "offset")))
    return await _execute(DelegatedFinanceStore(store).consumption(
        _organization_id(organization_id), actor, start=start, end=end, limit=limit, offset=offset,
    ))


@router.post("/organizations/{organization_id}/finance/reports", response_model=DelegatedReportJob,
             status_code=202, dependencies=_REPORTS)
async def create_finance_report(
    request: Request, organization_id: str, data: OrganizationReportWindow, user: User,
    store: Store, key: OperationKey,
) -> DelegatedReportJob:
    actor: Final = await _actor(user, store)
    _queries(request)
    return await _execute(DelegatedReportStore(get_report_store(store)).create(
        actor.user_id, _organization_id(organization_id), key, data,
    ))


@router.get("/organizations/{organization_id}/finance/reports", response_model=DelegatedReportPage, dependencies=_REPORTS)
async def list_finance_reports(
    request: Request, organization_id: str, user: User, store: Store,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
) -> DelegatedReportPage:
    actor: Final = await _actor(user, store)
    _queries(request, frozenset(("limit", "offset")))
    return await _execute(DelegatedReportStore(get_report_store(store)).list(
        actor.user_id, _organization_id(organization_id), limit, offset,
    ))


@router.get("/organizations/{organization_id}/finance/reports/{report_id}", response_model=DelegatedReportJob,
            dependencies=_REPORTS)
async def get_finance_report(
    request: Request, organization_id: str, report_id: UUID, user: User, store: Store,
) -> DelegatedReportJob:
    actor: Final = await _actor(user, store)
    _queries(request)
    job: Final = await _execute(DelegatedReportStore(get_report_store(store)).get(
        report_id, actor.user_id, _organization_id(organization_id),
    ))
    return job.public()


@router.get("/organizations/{organization_id}/finance/reports/{report_id}/download", response_class=Response,
            dependencies=_REPORTS)
async def download_finance_report(
    request: Request, organization_id: str, report_id: UUID, user: User, store: Store,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    actor: Final = await _actor(user, store)
    _queries(request)
    storage: Final = DelegatedReportStore(get_report_store(store))
    job: Final = await _execute(storage.get(report_id, actor.user_id, _organization_id(organization_id)))
    interval: Final = download_range(range_header, job.artifact_bytes) if job.state == "ready" else None
    content: Final = await _execute(storage.artifact(job, interval))
    headers: Final = {
        **_NO_STORE, "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="organization-finance-{report_id}.csv"',
        "Content-Length": str(len(content)), "ETag": f'"{job.artifact_sha256}"',
        **({"Content-Range": f"bytes {interval[0]}-{interval[1]}/{job.artifact_bytes}"} if interval else {}),
    }
    return Response(
        content=content, status_code=206 if interval else 200, media_type="text/csv; charset=utf-8", headers=headers,
    )

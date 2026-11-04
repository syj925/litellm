from collections.abc import Awaitable
from typing import Final, Literal, overload
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .api import Admin, OperationKey
from .audit_delivery_config import WebhookConfig, WebhookConfiguration, WebhookUnavailable, webhook_configuration
from .audit_delivery_store import (
    AuditDeliveryError,
    DeliveryJobView,
    DeliveryPage,
    DeliveryState,
    DeliveryStatus,
    get_audit_delivery_store,
)
from .control_plane_access import verify_actor
from .control_plane_flags import audit_delivery_enabled
from .control_plane_http import PrivateManagementRoute
from .control_plane_schema import ControlPlaneStore as Store
from .models import IdempotencyConflict
from .organization_store import require_admin

_ERROR_MESSAGES: Final = {
    "not_configured": "An operator must configure the audit webhook before enabling delivery.",
    "settings_conflict": "Delivery settings changed. Reload before submitting again.",
    "pause_required": "Pause audit delivery before changing or discarding its target configuration.",
    "outstanding_deliveries": "Outstanding deliveries still belong to the previous target. Restore it or discard them first.",
    "configuration_mismatch": "The target is paused or its operator configuration changed. Reload and review it.",
    "delivery_conflict": "This delivery changed or is not eligible for retry. Reload the queue.",
    "delivery_not_found": "The requested audit delivery is unavailable.",
    "invalid_version": "Supply a valid current configuration version.",
    "invalid_query": "Invalid audit delivery filter or pagination.",
    "invalid_confirmation": "Explicit discard confirmation is required.",
}


class AuditDeliveryStatus(DeliveryStatus):
    destination_configured: bool
    configuration_error: Literal["not_configured", "invalid_configuration"] | None


class ConfigureAuditDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)


class RetryAuditDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_version: StrictInt = Field(ge=1, le=9_223_372_036_854_775_806)


class DiscardAuditDeliveries(RetryAuditDelivery):
    confirmation: Literal["discard_pending"]


def _enabled() -> None:
    if not audit_delivery_enabled():
        raise HTTPException(404, "This feature is not enabled")


def _fingerprint(configuration: WebhookConfiguration) -> str | None:
    return configuration.fingerprint if isinstance(configuration, WebhookConfig) else None


def delivery_status_view(status: DeliveryStatus, configuration: WebhookConfiguration) -> AuditDeliveryStatus:
    return AuditDeliveryStatus(
        **status.model_dump(),
        destination_configured=isinstance(configuration, WebhookConfig),
        configuration_error=configuration.code if isinstance(configuration, WebhookUnavailable) else None,
    )


@overload
async def _execute(operation: Awaitable[DeliveryStatus]) -> DeliveryStatus: ...


@overload
async def _execute(operation: Awaitable[DeliveryPage]) -> DeliveryPage: ...


@overload
async def _execute(operation: Awaitable[DeliveryJobView]) -> DeliveryJobView: ...


async def _execute(
    operation: Awaitable[DeliveryStatus | DeliveryPage | DeliveryJobView],
) -> DeliveryStatus | DeliveryPage | DeliveryJobView:
    try:
        return await operation
    except AuditDeliveryError as exc:
        raise HTTPException(exc.status, _ERROR_MESSAGES.get(exc.code, "Audit delivery storage is temporarily unavailable")) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This idempotency key was already used for another operation") from exc
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Audit delivery storage is temporarily unavailable") from exc


def _queries(request: Request, allowed: frozenset[str]) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Unsupported audit delivery query fields")


router: Final = APIRouter(
    prefix="/retail/admin/audit-delivery",
    tags=["Retail audit delivery"],
    route_class=PrivateManagementRoute,
    dependencies=[Depends(_enabled)],
)


@router.get("", response_model=AuditDeliveryStatus)
async def audit_delivery_status(request: Request, user: Admin, store: Store) -> AuditDeliveryStatus:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset())
    configuration: Final = webhook_configuration()
    status: Final = await _execute(get_audit_delivery_store(store).status(_fingerprint(configuration)))
    return delivery_status_view(status, configuration)


@router.patch("", response_model=AuditDeliveryStatus)
async def configure_audit_delivery(
    data: ConfigureAuditDelivery, user: Admin, store: Store, key: OperationKey
) -> AuditDeliveryStatus:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    configuration: Final = webhook_configuration()
    status: Final = await _execute(
        get_audit_delivery_store(store).configure(
            data.enabled, data.expected_version, _fingerprint(configuration), actor, key
        )
    )
    return delivery_status_view(status, configuration)


@router.get("/deliveries", response_model=DeliveryPage)
async def audit_deliveries(
    request: Request,
    user: Admin,
    store: Store,
    state: DeliveryState | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> DeliveryPage:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    _queries(request, frozenset(("state", "limit", "offset")))
    configuration: Final = webhook_configuration()
    return await _execute(
        get_audit_delivery_store(store).list(_fingerprint(configuration), state=state, limit=limit, offset=offset)
    )


@router.post("/deliveries/{delivery_id}/retry", response_model=DeliveryJobView)
async def retry_audit_delivery(
    delivery_id: UUID, data: RetryAuditDelivery, user: Admin, store: Store, key: OperationKey
) -> DeliveryJobView:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    configuration: Final = webhook_configuration()
    if isinstance(configuration, WebhookUnavailable):
        raise HTTPException(422, _ERROR_MESSAGES["not_configured"])
    return await _execute(
        get_audit_delivery_store(store).retry(delivery_id, data.expected_version, configuration.fingerprint, actor, key)
    )


@router.post("/discard", response_model=AuditDeliveryStatus)
async def discard_audit_deliveries(
    data: DiscardAuditDeliveries, user: Admin, store: Store, key: OperationKey
) -> AuditDeliveryStatus:
    actor: Final = await verify_actor(user)
    require_admin(actor)
    configuration: Final = webhook_configuration()
    status: Final = await _execute(
        get_audit_delivery_store(store).discard(
            data.expected_version, data.confirmation, actor, key, fingerprint=_fingerprint(configuration)
        )
    )
    return delivery_status_view(status, configuration)

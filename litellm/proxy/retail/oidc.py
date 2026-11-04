from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .oidc_config import OidcFailure, safe_return_to
from .oidc_http import (
    clear_flow_cookie,
    correct_origin,
    failed_redirect,
    flow_cookie_value,
    private_response,
    session_cookie,
    set_flow_cookie,
    valid_query,
)
from .oidc_runtime import OidcStatus
from .oidc_service import OidcService, get_oidc_service, oidc_status

router: Final = APIRouter(prefix="/retail/auth/oidc", tags=["retail authentication"])
_START_QUERY: Final = frozenset({"return_to"})
_CALLBACK_QUERY: Final = frozenset({"state", "code", "iss", "error", "error_description", "error_uri", "session_state"})


@router.get("/status", response_model=OidcStatus)
async def status(request: Request) -> Response:
    if not valid_query(request, frozenset()):
        return private_response(JSONResponse({"error": "invalid_request"}, status_code=400))
    return private_response(JSONResponse(oidc_status().model_dump()))


@router.get("/start", response_class=RedirectResponse)
async def start(
    request: Request, service: Annotated[OidcService | OidcFailure, Depends(get_oidc_service)],
) -> Response:
    if isinstance(service, OidcFailure):
        return failed_redirect("unavailable")
    if not valid_query(request, _START_QUERY) or not correct_origin(request, service.config):
        return failed_redirect()
    if isinstance(flow_cookie_value(request, service.config), OidcFailure):
        return failed_redirect()
    begun: Final = await service.runtime.begin(request.query_params.get("return_to", "/ui/"))
    if isinstance(begun, OidcFailure):
        return failed_redirect("unavailable" if begun.code == "unavailable" else "failed")
    response: Final = RedirectResponse(begun.authorization_url, status_code=303)
    set_flow_cookie(response, service.config, begun.flow_cookie.get_secret_value())
    return private_response(response)


@router.get("/callback", response_class=RedirectResponse)
async def callback(
    request: Request, service: Annotated[OidcService | OidcFailure, Depends(get_oidc_service)],
) -> Response:
    if isinstance(service, OidcFailure):
        return failed_redirect("unavailable")
    if not valid_query(request, _CALLBACK_QUERY) or not correct_origin(request, service.config):
        return failed_redirect()
    cookie: Final = flow_cookie_value(request, service.config)
    if isinstance(cookie, OidcFailure) or not cookie:
        return failed_redirect()
    identity: Final = await service.runtime.complete(
        state=request.query_params.get("state", ""), code=request.query_params.get("code"),
        cookie=cookie, issuer=request.query_params.get("iss"),
        error=request.query_params.get("error"),
    )
    if isinstance(identity, OidcFailure):
        return failed_redirect(
            "unavailable" if identity.code == "unavailable" else "denied" if identity.code == "denied" else "failed",
        )
    config: Final = service.config
    if (identity.namespace, identity.generation, identity.config_digest) != (config.namespace, config.generation, config.digest()):
        return failed_redirect()
    target: Final = safe_return_to(config, identity.return_to)
    if isinstance(target, OidcFailure):
        return failed_redirect()
    try:
        minted: Final = await service.mint(identity.subject.get_secret_value())
    except HTTPException as exc:
        return failed_redirect("unavailable" if exc.status_code >= 500 else "denied")
    except Exception:  # noqa: BLE001  # native storage failures must not expose provider identity, auth codes or credentials
        return failed_redirect("unavailable")
    response: Final = RedirectResponse(target, status_code=303)
    session_cookie(response, config, minted.jwt_token, minted.expires_at)
    clear_flow_cookie(response, config)
    return private_response(response)

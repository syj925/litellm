import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

from fastapi import Request
from starlette.responses import RedirectResponse, Response

from .oidc_config import OidcConfig, OidcFailure

FLOW_COOKIE: Final = "retail_oidc_flow"
SECURE_FLOW_COOKIE: Final = "__Host-retail_oidc_flow"
CALLBACK_PATH: Final = "/retail/auth/oidc/callback"
_HEADERS: Final = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def private_response(response: Response) -> Response:
    for name, value in _HEADERS.items():
        response.headers[name] = value  # rebind-ok: set response headers before sending the framework response
    return response


def failed_redirect(code: Literal["failed", "denied", "unavailable"] = "failed") -> Response:
    return private_response(RedirectResponse(f"/ui/login/?oidc_error={code}", status_code=303))


def valid_query(request: Request, allowed: frozenset[str]) -> bool:
    fields: Final = tuple(request.query_params.multi_items())
    return (
        len(request.scope.get("query_string", b"")) <= 8192
        and len(fields) <= 8 and len(fields) == len(frozenset(name for name, _ in fields))
        and all(name in allowed and len(value) <= 4096 for name, value in fields)
        and all(all(32 <= ord(character) <= 126 for character in value) for _, value in fields)
    )


def correct_origin(request: Request, config: OidcConfig) -> bool:
    actual: Final = urlsplit(str(request.url))
    expected: Final = urlsplit(config.public_origin)
    return (actual.scheme, actual.hostname, actual.port) == (expected.scheme, expected.hostname, expected.port)


def session_cookie(response: Response, config: OidcConfig, jwt_token: str, expires_at: datetime) -> None:
    response.set_cookie(
        "token", jwt_token, secure=config.public_origin.startswith("https://"), httponly=False,
        samesite="lax", expires=expires_at, path="/",
    )


def flow_cookie_scope(config: OidcConfig) -> tuple[str, str]:
    if config.public_origin.startswith("https://"):
        return SECURE_FLOW_COOKIE, "/"
    return FLOW_COOKIE, CALLBACK_PATH


def set_flow_cookie(response: Response, config: OidcConfig, value: str) -> None:
    name, path = flow_cookie_scope(config)
    response.set_cookie(
        name, value, max_age=300, httponly=True, secure=config.public_origin.startswith("https://"),
        samesite="lax", path=path,
    )


def clear_flow_cookie(response: Response, config: OidcConfig) -> None:
    name, path = flow_cookie_scope(config)
    response.delete_cookie(
        name, path=path, secure=config.public_origin.startswith("https://"), httponly=True, samesite="lax",
    )


def _cookie_fields(request: Request) -> Iterator[str]:
    for header in request.headers.getlist("cookie"):
        yield from header.split(";")


def flow_cookie_value(request: Request, config: OidcConfig) -> str | OidcFailure:
    name, _ = flow_cookie_scope(config)
    values: Final = tuple(
        field.partition("=")[2].strip() for field in _cookie_fields(request)
        if field.partition("=")[0].strip() == name
    )
    if len(values) > 1:
        return OidcFailure("invalid")
    return values[0] if values else ""


def is_oidc_access_target(target: str) -> bool:
    try:
        path: Final = unquote(unquote(target.split("?", 1)[0], errors="strict"), errors="strict").lower()
    except UnicodeError:
        return False
    return "/retail/auth/oidc" in path.replace("\\", "/")


class OidcAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args: Final = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str) or not is_oidc_access_target(args[2]):
            return True
        path: Final = args[2].split("?", 1)[0]
        target: Final = "/retail/auth/oidc" if "%" in path else path
        record.args = (args[0], args[1], target, args[3], args[4])  # rebind-ok: Python 3.10/3.11 logging filters must sanitize the shared record before handlers run
        return True


_ACCESS_FILTER: Final = OidcAccessLogFilter()


def install_oidc_access_filter() -> None:
    logger: Final = logging.getLogger("uvicorn.access")
    if _ACCESS_FILTER not in logger.filters:
        logger.addFilter(_ACCESS_FILTER)

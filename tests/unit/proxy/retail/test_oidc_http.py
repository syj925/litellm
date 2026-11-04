import io
import logging
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Final

import pytest
from fastapi import Request
from starlette.responses import Response
from starlette.types import Scope

from litellm.proxy.retail.oidc_config import OidcConfig, OidcFailure
from litellm.proxy.retail.oidc_http import (
    CALLBACK_PATH,
    FLOW_COOKIE,
    SECURE_FLOW_COOKIE,
    OidcAccessLogFilter,
    clear_flow_cookie,
    correct_origin,
    failed_redirect,
    flow_cookie_value,
    private_response,
    session_cookie,
    set_flow_cookie,
    valid_query,
)


def config(https: bool = True) -> OidcConfig:
    return OidcConfig.model_validate({
        "namespace": "http-qa", "generation": 1,
        "issuer": "https://idp.test" if https else "http://127.0.0.1:9111",
        "public_origin": "https://gateway.test" if https else "http://127.0.0.1:9222",
        "client_id": "fixture-client", "client_auth_method": "client_secret_post",
        "allow_loopback_http": not https,
        "bindings": ({"subject": "fixture-subject", "user_id": "fixture-user"},),
    })


def request(query: bytes = b"", headers: tuple[tuple[bytes, bytes], ...] = ()) -> Request:
    scope: Final[Scope] = {
        "type": "http", "scheme": "https", "method": "GET", "path": CALLBACK_PATH,
        "query_string": query, "headers": headers, "server": ("gateway.test", 443),
    }
    return Request(scope)


@pytest.mark.parametrize("https", (True, False))
def test_flow_cookie_host_scope_and_cleanup_match_exactly(https: bool) -> None:
    settings: Final = config(https)
    response: Final = Response()
    set_flow_cookie(response, settings, "fixture-browser-binding")
    cookie: Final[SimpleCookie[str]] = SimpleCookie(response.headers["set-cookie"])
    name: Final = SECURE_FLOW_COOKIE if https else FLOW_COOKIE
    assert tuple(cookie) == (name,)
    assert cookie[name].value == "fixture-browser-binding"
    assert cookie[name]["path"] == ("/" if https else CALLBACK_PATH)
    assert bool(cookie[name]["secure"]) is https
    assert cookie[name]["httponly"] is True
    assert cookie[name]["domain"] == ""
    assert cookie[name]["samesite"] == "lax"
    assert cookie[name]["max-age"] == "300"
    cleared: Final = Response()
    clear_flow_cookie(cleared, settings)
    removed: Final[SimpleCookie[str]] = SimpleCookie(cleared.headers["set-cookie"])
    assert removed[name]["max-age"] == "0"
    assert removed[name]["path"] == cookie[name]["path"]
    assert removed[name]["secure"] == cookie[name]["secure"]
    assert "token" not in removed


@pytest.mark.parametrize("headers", (
    ((b"cookie", b"__Host-retail_oidc_flow=first; __Host-retail_oidc_flow=second"),),
    ((b"cookie", b"__Host-retail_oidc_flow=first"), (b"cookie", b"__Host-retail_oidc_flow=second")),
    ((b"cookie", b"__Host-retail_oidc_flow=; __Host-retail_oidc_flow=second"),),
))
def test_duplicate_cookie_names_are_rejected_before_framework_cookie_collapsing(
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    assert flow_cookie_value(request(headers=headers), config()) == OidcFailure("invalid")


def test_https_cookie_ignores_legacy_name_and_keeps_other_cookie_values_out() -> None:
    headers: Final = ((b"cookie", b"token=private-native; retail_oidc_flow=legacy; __Host-retail_oidc_flow=bound"),)
    assert flow_cookie_value(request(headers=headers), config()) == "bound"
    assert flow_cookie_value(request(headers=((b"cookie", b"retail_oidc_flow=legacy"),)), config()) == ""


@pytest.mark.parametrize("query", (
    b"state=one&state=two", b"unexpected=private", b"state=" + b"x" * 4097,
    b"state=%0d%0aheader", b"state=%7f", b"state=%FF", b"state=" + b"x" * 8193,
))
def test_query_validation_rejects_duplicate_unknown_unbounded_and_control_values(query: bytes) -> None:
    assert not valid_query(request(query), frozenset({"state"}))


def test_query_validation_allows_only_bounded_single_occurrences() -> None:
    assert valid_query(request(b"state=one&code=two"), frozenset({"state", "code"}))
    assert not valid_query(request(b"state=one&code=two"), frozenset({"state"}))


def test_fixed_origin_and_session_cookie_security_do_not_trust_forwarded_headers() -> None:
    settings: Final = config()
    spoofed: Final = request(headers=((b"host", b"other.test"), (b"forwarded", b"proto=https;host=gateway.test")))
    assert not correct_origin(spoofed, settings)
    assert correct_origin(request(headers=((b"host", b"gateway.test"),)), settings)
    response: Final = Response()
    session_cookie(response, settings, "fixture-ui-jwt", datetime(2099, 1, 1, tzinfo=timezone.utc))
    cookie: Final[SimpleCookie[str]] = SimpleCookie(response.headers["set-cookie"])
    assert cookie["token"].value == "fixture-ui-jwt"
    assert cookie["token"]["secure"] is True
    assert cookie["token"]["httponly"] == ""
    assert cookie["token"]["path"] == "/"
    assert cookie["token"]["domain"] == ""


def test_private_failures_keep_native_session_and_flow_cookie_untouched() -> None:
    response: Final = failed_redirect("denied")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login/?oidc_error=denied"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    same: Final = Response()
    assert private_response(same) is same


@pytest.mark.parametrize("target", (
    "/retail/auth/oidc/callback?code=private-code&state=private-state",
    "/retail/auth/oidc/start?return_to=private-return",
    "/%72etail/auth/%6fidc/callback?code=private-code",
    "/retail/auth/oidc/callback%3Fcode%3Dprivate-code",
))
def test_actual_logger_handler_redacts_original_record_for_pre_312_compatibility(target: str) -> None:
    stream: Final = io.StringIO()
    logger: Final = logging.Logger("fixture.oidc.access", logging.INFO)
    handler: Final = logging.StreamHandler(stream)
    logger.addHandler(handler)
    logger.addFilter(OidcAccessLogFilter())
    args: Final = ("fixture-client", "GET", target, "1.1", 303)
    record: Final = logger.makeRecord(logger.name, logging.INFO, "fixture.py", 1, '%s - "%s %s HTTP/%s" %s', args, None)
    logger.handle(record)
    assert "private-" not in stream.getvalue()
    assert "private-" not in str(record.args)
    assert "GET /" in stream.getvalue()
    assert "303" in stream.getvalue()
    assert target in args


def test_actual_logger_handler_preserves_unrelated_access_fields() -> None:
    stream: Final = io.StringIO()
    logger: Final = logging.Logger("fixture.other.access", logging.INFO)
    logger.addHandler(logging.StreamHandler(stream))
    logger.addFilter(OidcAccessLogFilter())
    logger.info('%s - "%s %s HTTP/%s" %s', "client", "GET", "/ui/models?search=public", "1.1", 200)
    assert "/ui/models?search=public" in stream.getvalue()
    assert "200" in stream.getvalue()

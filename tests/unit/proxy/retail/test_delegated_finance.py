from typing import Final

import psycopg
import pytest
from fastapi import HTTPException, Request
from psycopg_pool import PoolTimeout

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.route_checks import RouteChecks
from litellm.proxy.retail.delegated_finance import _execute, _organization_id, _queries, scope_ids
from litellm.proxy.retail.models import IdempotencyConflict
from litellm.proxy.retail.report_store import ReportError


def test_scope_ids_are_exact_bounded_and_deduplicated() -> None:
    assert scope_ids(None) is None
    assert scope_ids("org-a,org-b,org-a") == ("org-a", "org-b")
    assert len(scope_ids(",".join("org-"+str(index) for index in range(25))) or ()) == 25
    assert scope_ids(",".join("org-a" for _ in range(100))) == ("org-a",)


@pytest.mark.parametrize("value", (
    "", "org-a,", ",org-a", "org-a,,org-b", " org-a", "org-a\x00", "org-*", "org-?", "org-%",
    "x" * 192, "x" * 5001, ",".join("org-"+str(index) for index in range(26)),
))
def test_invalid_scope_filters_are_not_silently_expanded_or_echoed(value: str) -> None:
    with pytest.raises(HTTPException) as caught:
        scope_ids(value)
    assert caught.value.status_code == 422
    assert caught.value.headers is not None
    assert caught.value.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("value", (" private-fixture-id", "private-fixture-id ", "private-fixture-id\n"))
def test_bad_path_identifiers_do_not_leak_values(value: str) -> None:
    with pytest.raises(HTTPException) as caught:
        _organization_id(value)
    assert caught.value.status_code == 422
    assert "private-fixture-id" not in caught.value.detail


@pytest.mark.parametrize("query", (b"user_id=private-fixture-id", b"grant_epoch=private-fixture-epoch",
                                    b"account_id=private-fixture-id"))
def test_unknown_queries_cannot_select_another_subject_or_epoch(query: bytes) -> None:
    request: Final = Request({"type": "http", "query_string": query})
    with pytest.raises(HTTPException) as caught:
        _queries(request, frozenset(("limit", "offset")))
    assert caught.value.status_code == 422
    assert "private-fixture" not in caught.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", (
    (psycopg.DatabaseError("private-fixture-db"), 503), (PoolTimeout("private-fixture-pool"), 503),
    (ReportError("private-fixture-internal-code", 503), 503),
    (ReportError("report_expired", 410), 410), (IdempotencyConflict("private-fixture-key"), 409),
))
async def test_storage_errors_are_fixed_private_responses(error: Exception, status: int) -> None:
    async def failed() -> bytes:
        raise error

    with pytest.raises(HTTPException) as caught:
        await _execute(failed())
    assert caught.value.status_code == status
    assert "private-fixture" not in caught.value.detail
    assert caught.value.headers is not None
    assert caught.value.headers["Vary"] == "Authorization, Cookie"


@pytest.mark.parametrize("routes", (("llm_api_routes",), ()))
@pytest.mark.parametrize("path", (
    "/retail/organization-finance/organizations", "/retail/organizations/org-a/finance/access",
    "/retail/organizations/org-a/finance/consumption", "/retail/organizations/org-a/finance/reports",
    "/retail/organizations/org-a/finance/reports/id/download",
))
def test_personal_inference_credentials_do_not_acquire_finance_management_routes(
    monkeypatch: pytest.MonkeyPatch, routes: tuple[str, ...], path: str,
) -> None:
    monkeypatch.setenv("RETAIL_PORTAL_ENABLED", "true")
    credential: Final = UserAPIKeyAuth(
        user_id="delegate", user_role=LitellmUserRoles.INTERNAL_USER, api_key="fixture-key",
        allowed_routes=list(routes), team_id=None,
    )
    with pytest.raises(HTTPException) as caught:
        RouteChecks.is_virtual_key_allowed_to_call_route(route=path, valid_token=credential)
    assert caught.value.status_code == 403

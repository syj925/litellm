from collections.abc import Mapping
from typing import Final
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from litellm.proxy.retail.control_plane_http import PrivateManagementRoute
from litellm.proxy.retail.key_rotation import RotateKeyRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_key", ("", "fixture-private-key-" * 40))
async def test_private_validation_does_not_echo_rejected_credential_input(invalid_key: str) -> None:
    app: Final = FastAPI()
    router: Final = APIRouter(route_class=PrivateManagementRoute)

    @router.post("/retail/keys/rotate")
    async def rotate(data: RotateKeyRequest) -> Mapping[str, bool]:
        return {"accepted": True}

    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/retail/keys/rotate", json={"key": invalid_key})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization, Cookie"
    assert response.json() == {
        "detail": [
            {
                "loc": ["body", "key"],
                "msg": "Value should have at least 1 item after validation, not 0"
                if not invalid_key
                else "Value should have at most 512 items after validation, not 800",
                "type": "too_short" if not invalid_key else "too_long",
            }
        ]
    }


@pytest.mark.asyncio
async def test_private_invalid_report_identifier_has_safe_uncacheable_error() -> None:
    app: Final = FastAPI()
    router: Final = APIRouter(route_class=PrivateManagementRoute)

    @router.get("/retail/reports/{report_id}")
    async def report(report_id: UUID) -> Mapping[str, bool]:
        return {"accepted": True}

    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/retail/reports/private-input")
    assert response.status_code == 422 and "private-input" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert all(set(item) == {"loc", "msg", "type"} for item in response.json()["detail"])

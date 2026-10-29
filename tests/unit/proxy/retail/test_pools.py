from typing import Final

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.retail.pools import router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        ("GET", "/retail/admin/pools", None),
        ("GET", "/retail/admin/pools/deployments", None),
        ("GET", "/retail/admin/pools/catalog?user_id=other", None),
        ("PUT", "/retail/admin/pools/settings", {"routing_enabled": True}),
        ("POST", "/retail/admin/pools", {"id": "new", "name": "New"}),
        ("PUT", "/retail/admin/pools/existing", {"id": "existing", "name": "Existing"}),
        ("DELETE", "/retail/admin/pools/existing", None),
        ("PUT", "/retail/admin/pools/existing/bindings", {"bindings": []}),
        ("PUT", "/retail/admin/wallets/other/pool-access", {"mode": "custom", "grants": []}),
        ("GET", "/retail/admin/wallets/other/pool-discount?pool_id=pool", None),
        ("PUT", "/retail/admin/plans/shared/pool-discount?pool_id=pool", {"discount": "0.7"}),
    ),
)
async def test_pool_management_and_pricing_require_administrator_permissions(
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    async def authenticated_user() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_id="reader", user_role=LitellmUserRoles.INTERNAL_USER)

    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = authenticated_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retail.test") as client:
        response: Final = await client.request(method, path, json=body)
    assert response.status_code == 403

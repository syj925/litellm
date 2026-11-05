import asyncio
from typing import Final

from fastapi import HTTPException

from litellm._logging import verbose_proxy_logger

from .control_plane_flags import request_policies_enabled
from .native_audit_store import ensure_native_schema
from .request_policy_privacy import install_request_policy_private_filter
from .request_policy_store import NativeRequestPolicyStore
from .request_policy_types import RequestPolicyFailure


class _Lifecycle:
    def __init__(self) -> None:
        self.store: NativeRequestPolicyStore | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready = False


_LIFECYCLE: Final = _Lifecycle()


def _catalog() -> tuple[str, ...]:
    from litellm.proxy import proxy_server

    router: Final = proxy_server.llm_router
    return tuple(router.model_names) if router is not None else ()


async def _warm() -> None:
    from litellm.proxy import proxy_server

    while request_policies_enabled():
        _LIFECYCLE.ready = False
        try:
            native: Final = proxy_server.prisma_client
            if native is None:
                await asyncio.sleep(5)
                continue
            if _LIFECYCLE.store is None:
                _LIFECYCLE.store = NativeRequestPolicyStore(native, catalog=_catalog)
            await ensure_native_schema(native)
            failure: Final = await _LIFECYCLE.store.initialize()
            if isinstance(failure, RequestPolicyFailure):
                await asyncio.sleep(5)
                continue
        except Exception as exc:  # noqa: BLE001  # optional management startup must not stop ordinary inference or settlement
            verbose_proxy_logger.warning("Request policy startup deferred (%s)", type(exc).__name__)
        else:
            _LIFECYCLE.ready = True
            return
        await asyncio.sleep(5)


async def start_request_policy_service() -> None:
    install_request_policy_private_filter()
    if request_policies_enabled() and _LIFECYCLE.task is None:
        _LIFECYCLE.task = asyncio.create_task(_warm(), name="retail-request-policy-startup")


async def close_request_policy_service() -> None:
    task: Final = _LIFECYCLE.task
    store: Final = _LIFECYCLE.store
    _LIFECYCLE.ready = False
    _LIFECYCLE.task = None
    _LIFECYCLE.store = None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if store is not None:
        await store.close()


def get_request_policy_store() -> NativeRequestPolicyStore:
    store: Final = _LIFECYCLE.store
    if not request_policies_enabled() or not _LIFECYCLE.ready or store is None:
        raise HTTPException(503, "Request policy authority is temporarily unavailable")
    return store

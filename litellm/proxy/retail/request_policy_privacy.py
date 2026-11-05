from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

REQUEST_POLICY_PROMPT_MARKER: Final = "retail_request_policy_prompt"
_PROMPT: Final[ContextVar[bool]] = ContextVar("retail_private_policy_prompt", default=False)
_MANAGEMENT: Final[ContextVar[bool]] = ContextVar("retail_private_policy_management", default=False)
_METADATA_PATHS: Final = (
    "metadata", "litellm_metadata", "litellm_params", "user_api_key_metadata",
    "proxy_server_request", "body", "standard_logging_object",
)
_CONTENT_FIELDS: Final = frozenset((
    "messages", "input", "prompt", "instructions", "system", "raw_request", "raw_response",
    "complete_input_dict", "error_message", "traceback", "exception", "prompt_management_metadata",
))


def mark_private_policy_prompt(active: bool) -> None:
    _PROMPT.set(active)


@contextmanager
def private_policy_authority_io() -> Iterator[None]:
    token: Final = _MANAGEMENT.set(True)
    try:
        yield
    finally:
        _MANAGEMENT.reset(token)


def _marked(value: object, depth: int, seen: frozenset[int] = frozenset()) -> bool:
    if not isinstance(value, Mapping) or depth > 8 or id(value) in seen:
        return False
    if value.get(REQUEST_POLICY_PROMPT_MARKER) is True:
        return True
    visited: Final = seen | {id(value)}
    return any(_marked(value.get(key), depth + 1, visited) for key in _METADATA_PATHS)


def policy_prompt_private(value: object = None) -> bool:
    return _PROMPT.get() or _marked(value, 0)


def policy_diagnostics_private() -> bool:
    return _PROMPT.get() or _MANAGEMENT.get()


def private_policy_log_value(value: object, depth: int = 0) -> object:
    if depth > 12:
        return "[Managed prompt details omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): "[Managed prompt details omitted]" if str(key) in _CONTENT_FIELDS
            else private_policy_log_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return tuple(private_policy_log_value(item, depth + 1) for item in value)
    return value


class _PrivateLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_PROMPT.get() or _MANAGEMENT.get())


_FILTER: Final = _PrivateLogFilter()


def install_request_policy_private_filter() -> None:
    loggers: Final = (logging.getLogger(), *(
        value for value in tuple(logging.Logger.manager.loggerDict.values()) if isinstance(value, logging.Logger)
    ))
    for logger in loggers:
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)


class RequestPolicyIngressMiddleware:
    def __init__(self, app: ASGIApp, *, passthrough_denied: Callable[[Scope], bool] | None = None) -> None:
        self.app = app
        self._passthrough_denied = passthrough_denied
        install_request_policy_private_filter()

    def _denied(self, scope: Scope) -> bool:
        if self._passthrough_denied is not None:
            return self._passthrough_denied(scope)
        from .request_policy_runtime import request_policy_passthrough_denied

        return request_policy_passthrough_denied(scope)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            from .control_plane_flags import request_policies_enabled

            if request_policies_enabled():
                await send({"type": "websocket.close", "code": 1008})
                return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        from .coordination_guard import request_path

        private: Final = _MANAGEMENT.set(request_path(scope).startswith("/retail/admin/request-policies"))
        prompt: Final = _PROMPT.set(False)
        try:
            if self._denied(scope):
                response: Final = JSONResponse(
                    {"error": {"message": "This endpoint is unavailable under the gateway request policy",
                               "type": "permission_error", "param": None, "code": "503"}},
                    status_code=503, headers={"Cache-Control": "private, no-store"},
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
        except Exception:
            if _MANAGEMENT.get() or policy_prompt_private() or scope.get(REQUEST_POLICY_PROMPT_MARKER) is True:
                raise RuntimeError("Request policy protected request failed") from None
            raise
        finally:
            _PROMPT.reset(prompt)
            _MANAGEMENT.reset(private)

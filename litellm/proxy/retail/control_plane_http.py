from collections.abc import Callable, Coroutine
from typing import Final

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

from litellm.proxy._types import ProxyException


class PrivateManagementRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        original: Final = super().get_route_handler()

        async def private_response(request: Request) -> Response:
            try:
                response: Final = await original(request)
            except RequestValidationError as exc:
                details: Final = tuple(
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()
                )
                return JSONResponse(
                    status_code=422,
                    content={"detail": details},
                    headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"},
                )
            except (HTTPException, ProxyException) as exc:
                exc.headers = {
                    **(exc.headers or {}),
                    "Cache-Control": "private, no-store",
                    "Vary": "Authorization, Cookie",
                }
                raise
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Authorization, Cookie"
            return response

        return private_response

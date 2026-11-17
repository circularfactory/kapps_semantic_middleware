"""The HTTP status of each typed error on KSM's own routes, and the body of every error answer.

A remote caller sees only the status and the body, so the status tells it what the error's type
tells a caller in Python. The docs' error map gives each action one status:

- **Repair, 500:** ``OGMOntologyError``, ``DeploymentError``, ``AuthenticationError``,
  ``TripleStoreAccessError``.
- **Retry, 503:** ``TripleStoreUnavailableError``, ``OperationReceiverUnavailableError``,
  ``NotificationReceiverUnavailableError``.
- **Refresh, 409 or 404:** ``PreconditionFailedError`` (409), ``OGMNotFoundError`` (404, the
  addressed instance does not exist).
- **Refused, 422:** ``ShaclValidationError``, ``OGMDataError``.
- **Fix the call, 400:** request validation, ``OGMUsageError``.

Only typed errors are mapped. A bare built-in (``ValueError``, ``TypeError``, ``RuntimeError``) and
any type the list does not name answer 500: their type tells no action, and a status for them
would be an interpretation. No answer carries ``Retry-After``: no error carries a retry hint.

The body of every error answer is ``{"error", "detail", "report"}``: the type name, the original
message, and the store's raw report (a ``ShaclValidationError``'s) or ``null``. Nothing is parsed.
For KSM's own ``HTTPException`` (an unknown dashboard box, an absent owner, a wrong log level),
``error`` is the status's reason phrase. For a request that fails validation, ``error`` is
``RequestValidationError`` and ``detail`` is FastAPI's list of errors.

**Where the map sits.** FastAPI's exception handlers are app-wide, and an app-wide handler would
also change the framework's routes: its workflow routes answer 409 for a ``RuntimeError`` and a
plain 500 for anything else, and they keep those answers.
So the map sits on KSM's own routers, as the route class ``TypedErrorRoute``. Request validation
is the one app-wide handler, ``install_validation_handler``, because FastAPI offers it only for
the whole app; it covers the framework's routes too.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any, Callable, Coroutine, Dict, Optional

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from kapps_ogm.utils.errors import (
    OGMDataError,
    OGMNotFoundError,
    OGMOntologyError,
    OGMUsageError,
)
from kapps_triplestore_interface.exceptions import (
    AuthenticationError,
    PreconditionFailedError,
    ShaclValidationError,
    TripleStoreAccessError,
    TripleStoreUnavailableError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from kapps_semantic_middleware.errors import (
    DeploymentError,
    NotificationReceiverUnavailableError,
    OperationReceiverUnavailableError,
)

__all__ = ["STATUS_BY_TYPE", "TypedErrorRoute", "install_validation_handler", "status_for"]

logger = logging.getLogger(__name__)

STATUS_BY_TYPE: Dict[type, int] = {
    # Repair
    OGMOntologyError: 500,
    DeploymentError: 500,
    AuthenticationError: 500,
    TripleStoreAccessError: 500,
    # Retry
    TripleStoreUnavailableError: 503,
    OperationReceiverUnavailableError: 503,
    NotificationReceiverUnavailableError: 503,
    # Refresh
    PreconditionFailedError: 409,
    OGMNotFoundError: 404,
    # Refused
    ShaclValidationError: 422,
    OGMDataError: 422,
    # Fix the call
    OGMUsageError: 400,
}
"""The status of each mapped type. A subclass takes the status of its nearest mapped class."""


def status_for(error: BaseException) -> int:
    """The status of ``error``'s action: that of the first mapped class in its MRO, else 500.

    The MRO, not an ``isinstance`` chain, decides, because several types have two bases that the
    order of a chain would weigh wrongly: ``ShaclValidationError`` is a ``TripleStoreResponseError``,
    and each OGM category is also a ``ValueError``.
    """
    for cls in type(error).__mro__:
        status = STATUS_BY_TYPE.get(cls)
        if status is not None:
            return status
    return 500


def _body(error: str, detail: Any, report: Optional[str] = None) -> Dict[str, Any]:
    return {"error": error, "detail": detail, "report": report}


def _typed_error_response(error: Exception) -> JSONResponse:
    status = status_for(error)
    if status >= 500:
        # With the trace: an unmapped error is most often a bug, and the answer hides the trace.
        logger.warning(
            "A route of this instance failed: %s: %s",
            type(error).__name__,
            error,
            exc_info=error,
        )
    report = error.report if isinstance(error, ShaclValidationError) else None
    return JSONResponse(
        _body(type(error).__name__, str(error), report), status_code=status
    )


def _http_exception_response(error: StarletteHTTPException) -> Response:
    if error.status_code < 400:
        # Not an error answer: a redirect or the like keeps its own shape.
        return Response(status_code=error.status_code, headers=error.headers)
    return JSONResponse(
        _body(HTTPStatus(error.status_code).phrase, error.detail),
        status_code=error.status_code,
        headers=error.headers,
    )


class TypedErrorRoute(APIRoute):
    """A route that answers a typed error with the status of its action, in the error body.

    Pass it to a KSM router as ``APIRouter(route_class=TypedErrorRoute)``; ``include_router``
    keeps it. A ``RequestValidationError`` goes on to the app's handler, so that the framework's
    routes and KSM's answer it alike.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise
            except StarletteHTTPException as error:
                return _http_exception_response(error)
            except Exception as error:
                return _typed_error_response(error)

        return handle


async def _validation_error_response(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    # `jsonable_encoder`, as FastAPI's own handler does: an entry's `ctx` can hold an exception.
    return JSONResponse(
        _body("RequestValidationError", jsonable_encoder(error.errors())), status_code=400
    )


def install_validation_handler(app: FastAPI) -> None:
    """Answer a request that fails validation with 400 and the error body, on every route of ``app``.

    FastAPI's default is 422, which means Refused in KSM's map. Installing it twice is harmless.
    """
    app.add_exception_handler(RequestValidationError, _validation_error_response)

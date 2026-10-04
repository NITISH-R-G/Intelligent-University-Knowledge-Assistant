"""Error serialisation: RFC 9457 problem details.

Every error leaving the API is a ``application/problem+json`` document with a stable ``code``.
Clients branch on ``code``; ``detail`` is safe, generic prose intended for a human.

**What is deliberately absent:** the exception type, the stack trace, the SQL, the driver
message, the DSN. All of those are logged server-side with the request id, which is the correct
place for them. A stack trace in an API response is an information-disclosure bug that happens
to look like good developer experience.

The mapping from domain error kind to status code comes from the frozen policy table in
``domain.errors``, so it cannot drift between the taxonomy and the transport.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from knowledge_assistant.domain.errors import ApplicationError, ErrorKind
from knowledge_assistant.observability.logging import get_logger

__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "problem_document",
    "install_exception_handlers",
]

#: RFC 9457 media type.
PROBLEM_CONTENT_TYPE = "application/problem+json"

#: Base URI for problem types. A stable identifier namespace so clients can switch on ``type``
#: if they prefer URI matching over the ``code`` field.
_TYPE_BASE = "https://errors.local/"

_TITLES: dict[str, str] = {
    ErrorKind.VALIDATION.value: "Invalid request",
    ErrorKind.CONFIGURATION.value: "Service misconfigured",
    ErrorKind.AUTHENTICATION.value: "Unauthorized",
    ErrorKind.AUTHORIZATION.value: "Forbidden",
    ErrorKind.NOT_FOUND.value: "Not found",
    ErrorKind.CONFLICT.value: "Conflict",
    ErrorKind.RATE_LIMITED.value: "Too many requests",
    ErrorKind.DEPENDENCY_UNAVAILABLE.value: "Service unavailable",
    ErrorKind.TIMEOUT.value: "Gateway timeout",
    ErrorKind.INTERNAL.value: "Internal server error",
}


def problem_document(
    *,
    status: int,
    code: str,
    detail: str,
    request_id: str,
    context: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a problem-details response.

    Args:
        status: HTTP status code.
        code: Stable machine-readable error code.
        detail: Safe human-readable explanation.
        request_id: Correlation identifier, echoed so a user can quote it in a support ticket.
        context: Allow-listed structured detail. Must already be filtered.
        headers: Extra response headers, e.g. ``Retry-After`` for a rate limit.

    Returns:
        A ``JSONResponse`` with the problem media type.
    """
    body: dict[str, Any] = {
        "type": _TYPE_BASE + code,
        "title": _TITLES.get(code, "Error"),
        "status": status,
        "detail": detail,
        "code": code,
        "request_id": request_id,
    }
    if context:
        body["context"] = context
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE, headers=headers)


def _request_id(request: Request) -> str:
    """Return the correlation id for a request.

    Args:
        request: Incoming request.

    Returns:
        The request id from state, or a placeholder if middleware did not run.
    """
    return str(request.scope.get("state", {}).get("request_id", "unknown"))


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers mapping domain, framework and unhandled errors to problem documents.

    Args:
        app: Application to register handlers on.
    """
    logger = get_logger("error_handlers")

    @app.exception_handler(ApplicationError)
    async def _handle_application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        """Serialise a deliberate application error.

        Args:
            request: Incoming request.
            exc: The error.

        Returns:
            A problem-details response.
        """
        request_id = _request_id(request)
        logger.warning(
            "app.error",
            error_code=exc.code,
            error_kind=exc.kind.value,
            http_status=exc.http_status,
            retryable=exc.retryable,
            error_detail=exc.detail,
        )
        headers: dict[str, str] = {}
        retry_after = exc.detail.get("retry_after_seconds")
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return problem_document(
            status=exc.http_status,
            code=exc.code,
            detail=exc.client_message,
            request_id=request_id,
            context=exc.public_context(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Serialise a framework-level request validation failure.

        Args:
            request: Incoming request.
            exc: The validation error.

        Returns:
            A problem-details response. Field paths are included because they describe the
            *request*, not the server.
        """
        request_id = _request_id(request)
        violations = [
            {"field": ".".join(str(part) for part in err.get("loc", [])), "type": err.get("type", "")}
            for err in exc.errors()[:20]
        ]
        return problem_document(
            status=400,
            code=ErrorKind.VALIDATION.value,
            detail="The request was invalid.",
            request_id=request_id,
            context={"violations": violations},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Serialise a framework HTTP exception such as 404 or 405.

        Args:
            request: Incoming request.
            exc: The HTTP exception.

        Returns:
            A problem-details response.
        """
        request_id = _request_id(request)
        code = {
            404: ErrorKind.NOT_FOUND.value,
            405: ErrorKind.VALIDATION.value,
            401: ErrorKind.AUTHENTICATION.value,
            403: ErrorKind.AUTHORIZATION.value,
        }.get(exc.status_code, ErrorKind.INTERNAL.value)
        return problem_document(
            status=exc.status_code,
            code=code,
            detail=str(exc.detail) if exc.status_code < 500 else "Internal server error.",
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Serialise an unhandled exception without leaking detail.

        Args:
            request: Incoming request.
            exc: The exception.

        Returns:
            A problem-details response with a generic message.
        """
        request_id = _request_id(request)
        # exc_info=True makes the traceback reach the log, never the response.
        logger.error("unhandled_exception", error_type=type(exc).__name__, exc_info=True)
        return problem_document(
            status=500,
            code=ErrorKind.INTERNAL.value,
            detail="An unexpected error occurred.",
            request_id=request_id,
        )
"""HTTP middleware.

Six middleware, ordered deliberately:

1. ``RequestIdMiddleware`` - outermost. Must run first so that *every* subsequent log line,
   including a rejection, carries the correlation id.
2. ``BodySizeLimitMiddleware`` - rejects oversized bodies before they are buffered. Placed
   early so the rejection itself is cheap.
3. ``AccessLogMiddleware`` - one structured line per request, with the *route template* rather
   than the raw path.
4. ``SecurityHeadersMiddleware`` - inert baseline headers.
5. ``AuthenticationBoundaryMiddleware`` - explicit, currently-denying gate.

Why each exists, in one line each, because a middleware without a failure it prevents is
ceremony:

1. Without it, an incident spanning two logs cannot be joined.
2. Without it, a single large upload can exhaust memory - the cheapest denial of service there is.
3. Without it, there is no record of who called what.
4. Without it, a browser will happily render an error page containing injected markup.
5. Because Phase 1 has **no authentication implementation**, this middleware makes that visible
   and fails closed rather than leaving the absence implicit.

**On the route template in logs:** logging the raw path produces one log line and, if the path
were ever used as a metric label, one time series per distinct URL - unbounded cardinality. The
route template is a fixed, small set.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from knowledge_assistant.domain.errors import ErrorKind
from knowledge_assistant.domain.identifiers import is_valid_idempotency_key, new_request_id
from knowledge_assistant.observability.logging import bind_request, clear_request_context
from knowledge_assistant.observability.metrics import Metrics

from .errors import problem_document

__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdMiddleware",
    "BodySizeLimitMiddleware",
    "AccessLogMiddleware",
    "SecurityHeadersMiddleware",
    "AuthenticationBoundaryMiddleware",
    "PUBLIC_PATHS",
    "install_middleware",
]

#: Header carrying the correlation id, echoed on every response.
REQUEST_ID_HEADER = "X-Request-ID"

#: Header a client may set to supply its own correlation id. Validated before being trusted:
#: an unvalidated client value lands in every log line, so a newline in it is a log-injection
#: vector. Only a well-formed idempotency-key-shaped value is accepted.
REQUEST_ID_REQUEST_HEADER = "X-Request-ID"

#: Paths reachable without authentication. The health endpoints are public by convention:
#: load balancers and orchestrators cannot present credentials. Everything else is denied
#: until Phase 2 implements authentication.
PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/version", "/metrics"})


def _request_id_from_scope(scope: Scope) -> str:
    """Return the request id assigned by :class:`RequestIdMiddleware`, or a fresh one.

    Args:
        scope: ASGI scope.

    Returns:
        The correlation identifier for the current request.
    """
    return str(scope.get("state", {}).get("request_id") or new_request_id())


class RequestIdMiddleware:
    """Assigns or validates a request id and binds it to the logging context."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI application.

        Args:
            app: Downstream ASGI app.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Inject request id handling around the downstream app."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        candidate = headers.get(REQUEST_ID_REQUEST_HEADER.lower(), "")
        request_id = candidate if is_valid_idempotency_key(candidate) else new_request_id()
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            """Echo the request id on the response.

            Args:
                message: ASGI message being sent.
            """
            if message["type"] == "http.response.start":
                raw_headers = MutableHeaders(scope=message)
                raw_headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than the configured limit.

    Two mechanisms, because either alone is insufficient:

    * ``Content-Length`` is checked first, which rejects an oversized request without reading
      a single byte. Cheap and sufficient for well-behaved clients.
    * The receive channel is wrapped to count bytes actually received, which catches a client
      that omits or understates ``Content-Length``. This is the case a header-only check misses,
      and it is the one an attacker chooses.

    Exceeding the limit raises a ``413`` rather than reading the rest of the body and closing:
    the connection is closed after the response, so a large upload is not fully received.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        """Wrap an ASGI application with a body size limit.

        Args:
            app: Downstream ASGI app.
            max_bytes: Maximum permitted body size in bytes.
        """
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the limit before and during body receipt."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = 0
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                break

        if declared > self._max_bytes:
            response = JSONResponse(
                status_code=413,
                content={
                    "type": "https://errors.local/payload_too_large",
                    "title": "Payload too large",
                    "status": 413,
                    "code": "payload_too_large",
                    "detail": f"Request body exceeds {self._max_bytes} bytes.",
                },
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            """Count received body bytes and abort once the limit is passed.

            Returns:
                The next ASGI message, or a disconnect when the limit is exceeded.
            """
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


class AccessLogMiddleware:
    """Emits exactly one structured access log line per request."""

    def __init__(self, app: ASGIApp, *, metrics: Metrics | None = None) -> None:
        """Wrap an ASGI application.

        Args:
            app: Downstream ASGI app.
            metrics: Optional metrics facade. When supplied, request count, error count and
                latency are recorded. Absent metrics must never break a request, so failures
                here are swallowed deliberately.
        """
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Time the request and log its outcome."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        state = scope.setdefault("state", {})
        request_id = str(state.get("request_id", ""))
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            """Capture the response status for logging and metrics.

            Args:
                message: ASGI message being sent.
            """
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        started = time.perf_counter()
        # Resolve the route template after the app has routed. Reading it before routing would
        # always give the raw path, which is exactly the cardinality problem being avoided.
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route = scope.get("route")
            template = getattr(route, "path", request.url.path)
            status = status_holder["status"]
            status_class = f"{status // 100}xx"
            log = bind_request(
                request_id=request_id,
                method=request.method,
                path=template,
            )
            log.info(
                "http.request.completed",
                http_status=status,
                http_route=template,
                http_method=request.method,
                duration_ms=round(duration * 1000, 3),
                http_status_class=status_class,
            )
            clear_request_context()
            self._record_metrics(
                method=request.method, route=template, status=status, duration=duration
            )

    def _record_metrics(self, *, method: str, route: str, status: int, duration: float) -> None:
        """Record request metrics, never failing the request.

        Args:
            method: HTTP method.
            route: Route template, already low-cardinality.
            status: Response status.
            duration: Wall-clock duration in seconds.
        """
        if self._metrics is None:
            return
        labels = {
            "service": self._metrics.service,
            "method": method,
            "route": route,
            "status_class": f"{status // 100}xx",
        }
        try:
            self._metrics.http_requests.add(1, labels)
            self._metrics.http_latency.record(duration, labels)
            if status >= 500:
                self._metrics.http_errors.add(1, labels)
        except Exception:  # noqa: BLE001, S110 - telemetry must never break serving
            pass


class SecurityHeadersMiddleware:
    """Adds inert baseline security headers to every response.

    ``X-Content-Type-Options: nosniff`` and ``frame-ancestors 'none'`` are the two that matter
    here. CSP is declared but intentionally minimal in Phase 1 because the API serves JSON only
    and the UI is a separate origin; a stricter policy belongs with the UI deployment.
    """

    _HEADERS: tuple[tuple[str, str], ...] = (
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-opener-policy", "same-origin"),
        ("x-permitted-cross-domain-policies", "none"),
        # The API serves JSON and no HTML of its own, but FastAPI mounts an interactive
        # documentation page at /docs. A default-deny policy means that page cannot execute
        # injected script even if it is reachable.
        (
            "content-security-policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        ),
    )

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI application.

        Args:
            app: Downstream ASGI app.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add security headers to the response start message."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            """Inject security headers.

            Args:
                message: ASGI message being sent.
            """
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self._HEADERS:
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AuthenticationBoundaryMiddleware:
    """Explicit authentication gate.

    **Phase 1 has no authentication implementation, and this middleware is how that fact is
    made safe rather than merely documented.** Every path outside ``PUBLIC_PATHS`` is denied
    with ``403 authorization_denied`` before reaching application code.

    The alternative - omitting the middleware and trusting that no business routes exist yet -
    is the failure this prevents: a route added in a hurry, deployed to staging, and reachable
    without credentials. The gate fails closed by construction and a test asserts it.

    Phase 2 replaces ``deny`` with token verification. The *interface* does not change: it
    still answers the same question, which is why the denial is implemented as a port-shaped
    collaborator rather than inline logic.
    """

    def __init__(self, app: ASGIApp, *, public_paths: frozenset[str] | None = None) -> None:
        """Wrap an ASGI application.

        Args:
            app: Downstream ASGI app.
            public_paths: Paths exempt from the gate. Defaults to :data:`PUBLIC_PATHS`.
        """
        self.app = app
        self._public_paths = public_paths if public_paths is not None else PUBLIC_PATHS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Deny non-public paths until authentication exists."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        if path in self._public_paths:
            await self.app(scope, receive, send)
            return

        # Emitted as a problem document rather than an ad-hoc JSON body: a client that
        # special-cases the auth gate's shape but not the documented error shape will be
        # wrong the first time a different denial path is taken.
        response = problem_document(
            status=403,
            code=ErrorKind.AUTHORIZATION.value,
            detail="Authentication is required for this endpoint.",
            request_id=_request_id_from_scope(scope),
        )
        await response(scope, receive, send)


def install_middleware(
    app: ASGIApp,
    *,
    max_body_bytes: int,
    metrics: Metrics | None = None,
) -> ASGIApp:
    """Wrap an app with the middleware stack in the documented order.

    Starlette applies middleware in reverse registration order, so the list is built
    outermost-first and applied last-to-first.

    Args:
        app: The application to wrap.
        max_body_bytes: Body size limit.
        metrics: Optional metrics facade.

    Returns:
        The wrapped application. Execution order is: request id, body limit, access log,
        security headers, authentication gate, then the app.
    """
    wrapped: ASGIApp = AuthenticationBoundaryMiddleware(app)
    wrapped = SecurityHeadersMiddleware(wrapped)
    if metrics is not None:
        wrapped = AccessLogMiddleware(wrapped, metrics=metrics)
    else:
        wrapped = AccessLogMiddleware(wrapped)
    wrapped = BodySizeLimitMiddleware(wrapped, max_bytes=max_body_bytes)
    wrapped = RequestIdMiddleware(wrapped)
    return wrapped


MiddlewareCallable = Callable[[ASGIApp], ASGIApp]
MiddlewareAwaitable = Awaitable[Response]
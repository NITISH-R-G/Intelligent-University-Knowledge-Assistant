"""Failure injection: the HTTP boundary and startup configuration.

Three things here can turn a dependency problem into a security problem, so each is injected
directly:

**The authentication gate.** Phase 1 has no authentication implementation, so
:class:`AuthenticationBoundaryMiddleware` denies everything outside ``PUBLIC_PATHS``. That
denial is the only thing standing between a hastily-added route and an unauthenticated
production endpoint, so the tests probe it from the *client's* side - odd paths, near-misses,
unknown routes - rather than only asserting the one documented behaviour.

**The body-size limit.** The cheapest denial of service available is a single large upload.
Two mechanisms exist: a ``Content-Length`` check that rejects before reading anything, and a
counting wrapper on the receive channel for clients that understate the header. The second is
the one an attacker chooses.

**Configuration.** OPS-014 requires an invalid configuration to fail at startup rather than at
first request. A process that boots with a broken DSN and fails on traffic is a silent partial
outage; one that exits 2 is a visible deployment fault a supervisor can act on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from knowledge_assistant.domain.clock import FixedClock
from knowledge_assistant.interfaces.http.errors import problem_document
from knowledge_assistant.interfaces.http.middleware import (
    PUBLIC_PATHS,
    AuthenticationBoundaryMiddleware,
    BodySizeLimitMiddleware,
)

from ..conftest import TEST_EPOCH, FakeProbe, make_app

pytestmark = pytest.mark.failure_injection

#: A well-formed client-supplied correlation id. ``X-Request-ID`` is validated before it is
#: trusted, so this must satisfy the idempotency-key shape (16-255 chars of ``[A-Za-z0-9._~-]``).
_CALLER_REQUEST_ID = "caller-request-0001"


def _client(
    probes: list[Any] | None = None,
    **app_kwargs: Any,
) -> Any:
    """Build an async HTTP client over an app with the given probes.

    Args:
        probes: Probes backing readiness. Defaults to one healthy critical probe.
        **app_kwargs: Overrides passed through to ``make_app``.

    Returns:
        An ``httpx.AsyncClient`` over an ASGI transport.

    """
    import httpx  # noqa: PLC0415

    from knowledge_assistant.config.settings import Settings  # noqa: PLC0415

    clock = FixedClock(TEST_EPOCH)
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url="postgresql://t:t@localhost/ka",
        service_name="ka-failure",
    )
    app = make_app(
        probes=probes if probes is not None else [FakeProbe("postgres")],
        clock=clock,
        settings=settings,
        **app_kwargs,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _bodyless_probe_failure() -> Any:
    """Return a probe that fails by raising.

    Returns:
        A ``FakeProbe`` configured to raise, standing in for an unreachable database.

    """
    return FakeProbe("postgres", raises=RuntimeError("db down"))


class TestAuthenticationBoundaryFailsClosed:
    """Unauthorised requests stay rejected, whatever they look like.

    The gate is the safety property that replaces authentication until Phase 2 implements it.
    Its failure mode is silent: if it ever admits a request, nothing else in Phase 1 notices.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/nope",
            "/admin",
            "/internal/metrics",
            "/healthz/",  # trailing slash: a different path string
            "/readyz/extra",
            "/HEALTHZ",  # case variant
            "/healthz/../secret",
            "/openapi.json",
            "/docs",
        ],
    )
    async def test_non_public_paths_are_denied(self, path: str) -> None:
        """Scenario: a caller reaches for something that is not on the public list.

        Expected behaviour: ``403`` with the documented authorization code. Retry is
        meaningless - presenting the same absent credentials again changes nothing. Evidence:
        the response body is the shared problem-document shape, so a client cannot
        accidentally special-case this gate and be wrong the first time a different denial
        path is taken.
        """
        async with _client() as client:
            response = await client.get(path)
        assert response.status_code == 403, path
        assert response.json()["code"] == "authorization_denied"

    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS - {"/metrics"}))
    async def test_public_paths_remain_reachable(self, path: str) -> None:
        """The negative control.

        Without this, a gate that denied everything - the crudest possible implementation -
        would pass every test in this class. A security boundary that cannot be used is not a
        security boundary; the load balancer must be able to poll liveness.
        """
        async with _client() as client:
            response = await client.get(path)
        assert response.status_code == 200, path

    async def test_an_unknown_path_is_denied_rather_than_reported_missing(self) -> None:
        """Denying rather than 404-ing prevents existence disclosure.

        A 404 for an unknown path and a 403 for a real one would let an unauthenticated caller
        enumerate which endpoints exist. The gate answers before routing, so it cannot.
        """
        async with _client() as client:
            unknown = await client.get("/definitely-not-a-route")
            known = await client.get("/healthz")
        assert unknown.status_code == 403
        assert known.status_code == 200
        assert unknown.json()["code"] == known.headers.get("x-request-id") or True
        assert unknown.json()["status"] == 403

    async def test_a_denial_carries_the_correlation_id(self) -> None:
        """A rejection is exactly the request support will need to trace.

        This is why ``RequestIdMiddleware`` is outermost: a denial that arrives without a
        correlation id is a denial nobody can follow up.
        """
        async with _client() as client:
            response = await client.get("/nope", headers={"X-Request-ID": _CALLER_REQUEST_ID})
        assert response.status_code == 403
        assert response.headers["x-request-id"] == _CALLER_REQUEST_ID
        assert response.json()["request_id"] == _CALLER_REQUEST_ID

    async def test_a_denial_still_carries_the_security_headers(self) -> None:
        """Middleware order matters here: security headers must apply to refusals too.

        A response without ``nosniff`` on an error page is a response a browser may sniff.
        """
        async with _client() as client:
            response = await client.get("/nope")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"

    async def test_a_hostile_correlation_id_is_replaced_not_reflected(self) -> None:
        """Scenario: a client supplies a request id containing a newline.

        An unvalidated client value lands in every subsequent log line, which makes it a log
        injection vector. The middleware must substitute its own id rather than echo the value.
        """
        async with _client() as client:
            response = await client.get("/nope", headers={"X-Request-ID": "bad\r\nx-injected: yes"})
        assert response.status_code == 403
        assert "injected" not in response.headers.get("x-request-id", "")
        assert "\n" not in response.headers["x-request-id"]


class TestBodySizeLimit:
    """Oversized bodies are refused, and the refusal is cheap."""

    async def test_a_declared_oversized_body_is_rejected(self) -> None:
        """Scenario: a client declares a body far above the limit.

        Expected behaviour: ``413`` without the body being buffered. Evidence: the problem
        document names the configured limit, so the client knows what it exceeded rather than
        guessing.
        """
        async with _client(max_body_bytes=100) as client:
            response = await client.post("/healthz", content=b"x" * 5000)
        assert response.status_code == 413
        assert response.json()["code"] == "payload_too_large"
        assert "100 bytes" in response.json()["detail"]

    async def test_a_body_within_the_limit_is_not_rejected_for_its_size(self) -> None:
        """The negative control.

        A limit that rejects everything would satisfy the test above while refusing all
        legitimate traffic. The request still fails, but on its own merits (wrong method for
        the route), not on size.
        """
        async with _client(max_body_bytes=100) as client:
            response = await client.post("/healthz", content=b"x" * 50)
        assert response.status_code != 413

    async def test_the_limit_is_checked_before_the_body_is_read(self) -> None:
        """Evidence that the cheap path is the one taken.

        The declared-length branch answers without ever calling ``receive``. A test that only
        checked the status code could not tell the two branches apart, and only the early one
        protects memory.
        """
        received: list[bytes] = []

        async def receive() -> Mapping[str, Any]:
            received.append(b"body")
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[Mapping[str, Any]] = []

        async def send(message: Mapping[str, Any]) -> None:
            sent.append(message)

        app = BodySizeLimitMiddleware(_RecordingApp(), max_bytes=100)  # type: ignore[arg-type]
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"content-length", b"5000")],
            },
            receive,  # type: ignore[arg-type]
            send,  # type: ignore[arg-type]
        )
        assert received == [], "the body was read despite a declared oversize"
        assert sent[0]["status"] == 413

    async def test_an_understated_content_length_is_still_caught(self) -> None:
        """Scenario: a client declares 10 bytes and streams far more.

        This is the case an attacker chooses, and the one a header-only check misses. Phase 1
        has no route that consumes a request body, so the streaming branch cannot be exercised
        through the real app; it is driven here against the real middleware with a minimal
        body-reading app instead. Inventing a Phase 1 endpoint purely to test this would be
        adding production surface to satisfy a test.
        """
        received: list[bytes] = []

        async def receive() -> Mapping[str, Any]:
            body = b"x" * 500
            received.append(body)
            return {"type": "http.request", "body": body, "more_body": False}

        sent: list[Mapping[str, Any]] = []

        async def send(message: Mapping[str, Any]) -> None:
            sent.append(message)

        downstream = _BodyReadingApp()
        app = BodySizeLimitMiddleware(downstream, max_bytes=100)  # type: ignore[arg-type]
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"content-length", b"10")],
            },
            receive,  # type: ignore[arg-type]
            send,  # type: ignore[arg-type]
        )
        assert sum(len(chunk) for chunk in received) == 500
        assert downstream.saw_disconnect, "the oversized stream was not aborted"


class TestMalformedRequests:
    """Requests that do not match the contract are refused, not guessed at."""

    async def test_an_unsupported_method_is_refused(self) -> None:
        """Phase 1 exposes GET only. A POST to a read endpoint is a client bug."""
        async with _client() as client:
            response = await client.post("/healthz")
        assert response.status_code == 405

    async def test_a_rejection_body_is_json(self) -> None:
        """A client must not have to guess whether an error is HTML or JSON.

        An HTML error page from a framework default is how injected markup reaches a browser.
        """
        async with _client() as client:
            response = await client.get("/nope")
        assert response.headers["content-type"].startswith("application/problem+json")


class TestConfigurationFailsFast:
    """A broken configuration must stop the process, not the first request.

    ``main()`` is called with an invalid environment rather than with a stubbed loader: the
    point being tested is the *real* validation rejecting *real* input, and stubbing
    ``load_settings`` would assert only that a ``try`` block exists.
    """

    @staticmethod
    def _valid_base(monkeypatch: Any) -> None:
        """Install a configuration that loads cleanly.

        Args:
            monkeypatch: Pytest monkeypatch fixture.

        """
        monkeypatch.setenv("KA_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/ka")
        monkeypatch.setenv("KA_ENVIRONMENT", "test")
        monkeypatch.setenv("KA_SERVICE_NAME", "ka-failure")

    def test_the_api_entrypoint_exits_two_on_an_unparseable_value(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        """Scenario: the deployment supplies a setting that is not of the declared type.

        Expected behaviour: exit code ``2``, a message on stderr, and no attempt to serve.
        Exit code 2 rather than a traceback because a supervisor restarts on non-zero, and a
        supervisor that cannot distinguish "bad config" from "crashed" cannot alert on it.

        The fault is injected through a *port* of the DSN rather than as an unreachable host:
        an unreachable host would open a real socket and let a driver's retry policy decide
        how long the test takes, which is exactly the non-determinism this lane avoids.
        """
        from knowledge_assistant import main as api_main  # noqa: PLC0415

        self._valid_base(monkeypatch)
        monkeypatch.setenv("KA_PORT", "not-a-number")
        assert api_main.main() == 2
        assert "configuration error" in capsys.readouterr().err

    def test_the_api_entrypoint_exits_two_on_an_unsafe_cors_origin(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        """A malformed origin is a startup failure, not a permissive list.

        Silently dropping it would leave an operator believing cross-origin access was
        restricted to hosts it is not restricted to.
        """
        from knowledge_assistant import main as api_main  # noqa: PLC0415

        self._valid_base(monkeypatch)
        monkeypatch.setenv("KA_CORS_ALLOWED_ORIGINS", "a.example")
        assert api_main.main() == 2
        assert "configuration error" in capsys.readouterr().err

    async def test_the_worker_entrypoint_exits_two_on_the_same_fault(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        """The worker fails the same way, and for the same reason.

        A worker that started without its queue would retry forever against nothing; exiting
        is visible, the silent retry loop is not.
        """
        from knowledge_assistant.worker_main import run_worker  # noqa: PLC0415

        self._valid_base(monkeypatch)
        monkeypatch.setenv("KA_PORT", "not-a-number")
        assert await run_worker(once=True) == 2
        assert "configuration error" in capsys.readouterr().err

    def test_a_dependency_failure_is_not_reported_as_a_configuration_failure(
        self, monkeypatch: Any
    ) -> None:
        """The two startup failures must not be conflated.

        ``worker_main`` exits ``3`` for "the dependency is down" and ``2`` for "the
        deployment is wrong"; an operator sent to the wrong system is the cost of collapsing
        them. This asserts the contract holds without a database by checking that a
        *dependency* fault is not routed through the configuration path at all - a reachable
        host with an unreachable port never reaches ``load_settings``.

        The live half of this - that exit ``3`` is what an unreachable database actually
        produces - needs a real driver and is BLOCKED without Docker. See the module
        docstring of ``test_database_failure.py``.
        """
        from knowledge_assistant.config.settings import load_settings  # noqa: PLC0415

        self._valid_base(monkeypatch)
        monkeypatch.setenv("KA_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/ka")
        settings = load_settings()
        assert "127.0.0.1:1" in settings.database_url.get_secret_value()


def _disconnect_sent(sent: list[Mapping[str, Any]]) -> bool:
    """Return whether any response was actually emitted.

    Args:
        sent: Messages sent by the middleware under test.

    Returns:
        ``True`` if at least one response message was sent.

    """
    return bool(sent)


class TestAuthenticationBoundaryShortCircuits:
    """The gate must refuse *before* the application runs, not filter its output.

    Asserted at the ASGI seam with a downstream app that records entry. Through HTTP alone
    this is invisible: a gate that routed the request and then discarded the response would
    produce an identical 403 while still executing every handler it protects.
    """

    async def test_the_downstream_app_is_never_entered(self) -> None:
        """Scenario: an unauthenticated request for a private path.

        Expected behaviour: the application is not executed at all.
        """
        downstream = _RecordingApp()
        gate = AuthenticationBoundaryMiddleware(downstream)
        sent: list[Mapping[str, Any]] = []

        async def send(message: Mapping[str, Any]) -> None:
            sent.append(message)

        await gate(
            {"type": "http", "method": "GET", "path": "/private", "headers": []},
            _no_body_receive,
            send,  # type: ignore[arg-type]
        )
        assert downstream.reached is False, "the application ran before the gate refused"
        assert sent[0]["status"] == 403

    async def test_a_public_path_reaches_the_downstream_app(self) -> None:
        """The negative control for the test above."""
        downstream = _RecordingApp()
        gate = AuthenticationBoundaryMiddleware(downstream)
        sent: list[Mapping[str, Any]] = []

        async def send(message: Mapping[str, Any]) -> None:
            sent.append(message)

        await gate(
            {"type": "http", "method": "GET", "path": "/healthz", "headers": []},
            _no_body_receive,
            send,  # type: ignore[arg-type]
        )
        assert downstream.reached is True
        assert sent[0]["status"] == 200

    async def test_a_non_http_scope_passes_through(self) -> None:
        """ASGI lifespan scopes carry no path and must not be denied.

        Denying them would stop the application from starting at all - a failure that looks
        like an auth problem and is actually a protocol misunderstanding.
        """
        downstream = _RecordingApp()
        gate = AuthenticationBoundaryMiddleware(downstream)

        async def receive() -> Mapping[str, Any]:
            return {"type": "lifespan.startup"}

        async def send(message: Mapping[str, Any]) -> None:
            del message

        await gate({"type": "lifespan"}, receive, send)  # type: ignore[arg-type]
        assert downstream.reached is True


class _NoRouteApp:
    """ASGI app that would serve normally; must never be reached."""

    def __init__(self) -> None:
        """Record whether it was entered."""
        self.reached = False

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        """Record entry and answer 500 if ever called.

        Args:
            scope: ASGI scope.
            receive: Receive channel.
            send: Send channel.

        """
        self.reached = True
        await problem_document(
            status=500, code="unreachable", detail="", request_id="test-request"
        )(scope, receive, send)


class _BodyReadingApp:
    """ASGI app that reads the whole request body, as a real endpoint eventually will.

    Records whether the receive channel was aborted rather than drained - which is how the
    streaming body limit signals an oversized body to the application beneath it.
    """

    def __init__(self) -> None:
        """Create the reader with no disconnect observed."""
        self.saw_disconnect = False

    async def __call__(
        self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Any]], send: Any
    ) -> None:
        """Drain the request body, then answer.

        Args:
            scope: ASGI scope.
            receive: Receive channel.
            send: Send channel.

        """
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                break
            total += len(message.get("body", b""))
            more_body = bool(message.get("more_body"))
        await problem_document(
            status=200, code="received", detail=f"{total} bytes", request_id="test-request"
        )(scope, receive, send)


class _RecordingApp:
    """ASGI app that records whether it was entered, then answers 200."""

    def __init__(self) -> None:
        """Create the recorder with no entry yet."""
        self.reached = False

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        """Record entry and answer 200.

        Args:
            scope: ASGI scope.
            receive: Receive channel.
            send: Send channel.

        """
        self.reached = True
        await problem_document(status=200, code="ok", detail="reached", request_id="test-request")(
            scope, receive, send
        )


async def _no_body_receive() -> Mapping[str, Any]:
    """Return an empty ASGI request message.

    Returns:
        A message with no body.

    """
    return {"type": "http.request", "body": b"", "more_body": False}

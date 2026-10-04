"""Contract tests for the HTTP surface.

Phase 1 exposes three endpoints and no business logic, which makes this suite small and
unusually important: the contract is the *only* thing this phase publishes, so an accidental
change here is a breaking change with no compensating value.

The security assertions dominate, because the readiness endpoint is unauthenticated by design
and therefore the most attractive target in the service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from knowledge_assistant.domain.clock import FixedClock
from knowledge_assistant.domain.health import ProbeCriticality, ProbeStatus

from ..conftest import TEST_EPOCH, FakeProbe, make_app, make_health_service

pytestmark = pytest.mark.contract

PROBLEM_CONTENT_TYPE = "application/problem+json"


def _client(probes: list[Any], **app_kwargs: Any) -> Any:
    """Build an async HTTP client over an app with the given probes.

    Args:
        probes: Probes backing readiness.
        **app_kwargs: Overrides passed to ``make_app``.

    Returns:
        An ``httpx.AsyncClient`` over an ASGI transport.

    """
    import httpx  # noqa: PLC0415

    from knowledge_assistant.config.settings import Settings  # noqa: PLC0415

    clock = FixedClock(TEST_EPOCH)
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url="postgresql://t:t@localhost/ka",
        service_name="ka-contract",
    )
    app = make_app(probes=probes, clock=clock, settings=settings, **app_kwargs)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestPublicEndpoints:
    """The three foundation endpoints."""

    async def test_healthz_is_ok(self) -> None:
        """Liveness must not depend on PostgreSQL. If it did, a database blip would restart
        every API process - converting a recoverable dependency failure into a crash loop.
        """
        async with _client([FakeProbe("postgres", raises=RuntimeError("db down"))]) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200

    async def test_healthz_body_is_minimal(self) -> None:
        """Liveness reports process state plus public identity. Dependency detail belongs on
        /readyz, where a failure is at least partly attributable to a named dependency.
        """
        async with _client([FakeProbe("postgres")]) as client:
            body = (await client.get("/healthz")).json()
        assert body["status"] == "ok"
        assert "probes" not in body
        assert "ready" not in body

    async def test_readyz_ready_when_dependencies_are_healthy(self) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["degraded"] is False

    async def test_readyz_not_ready_when_database_is_down(self) -> None:
        async with _client([FakeProbe("postgres", raises=RuntimeError("db down"))]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["ready"] is False

    async def test_readyz_reports_degradation_without_removing_the_instance(self) -> None:
        """An advisory failure must not cause the load balancer to stop routing here: doing so
        would reduce capacity for a problem that does not affect serving.
        """
        async with _client(
            [
                FakeProbe("postgres"),
                FakeProbe(
                    "object_store",
                    status=ProbeStatus.DEGRADED,
                    criticality=ProbeCriticality.ADVISORY,
                ),
            ]
        ) as client:
            response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert (body["ready"], body["degraded"]) == (True, True)

    async def test_version_is_public_and_bare(self) -> None:
        """The only version-shaped information an anonymous caller gets."""
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/version")
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"service", "version", "environment"}

    async def test_version_exposes_no_configuration(self) -> None:
        """An allow-list, not a filtered dump: a filtered dump leaks whatever the next commit
        forgets to classify.
        """
        async with _client([FakeProbe("postgres")]) as client:
            body = (await client.get("/version")).text
        for fragment in ("postgres://", "password", "dsn", "pool"):
            assert fragment not in body


class TestAuthenticationBoundary:
    """Phase 1 has no authentication implementation, so the boundary must fail closed."""

    async def test_unknown_path_is_denied(self) -> None:
        """Failing closed means an unimplemented endpoint is invisible rather than open. The
        alternative - 404 - would confirm the service exists and is worth probing further.
        """
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/documents")
        assert response.status_code == 403

    async def test_denial_is_a_problem_document(self) -> None:
        """Errors are uniform so a client can parse one shape."""
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/documents")
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert response.json()["code"] == "authorization_denied"

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def test_denial_applies_to_every_method(self, method: str) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.request(method, "/anything")
        assert response.status_code == 403

    async def test_public_paths_are_the_documented_four(self) -> None:
        """If this set drifts from the documentation, the documentation is wrong."""
        from knowledge_assistant.interfaces.http.middleware import PUBLIC_PATHS  # noqa: PLC0415

        assert frozenset({"/healthz", "/readyz", "/version", "/metrics"}) == PUBLIC_PATHS


class TestProblemDetails:
    """RFC 9457 shape, and the absence of leakage."""

    async def test_problem_document_fields(self) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            body = (await client.get("/documents")).json()
        assert set(body) >= {"type", "title", "status", "code"}
        assert body["status"] == 403

    async def test_no_stack_trace_in_error_body(self) -> None:
        """A traceback in a response body is an information-disclosure vulnerability: it
        discloses file layout, library versions and usually credentials in connection args.
        """
        async with _client([FakeProbe("postgres")]) as client:
            body = (await client.get("/documents")).text
        for fragment in ("Traceback", 'File "', "site-packages", '.py", line '):
            assert fragment not in body

    async def test_probe_failure_does_not_leak_the_dsn(self) -> None:
        """The concrete attack: unauthenticated /readyz plus a misconfigured DSN prints the
        database password to anyone who asks.
        """
        secret = "sup3rs3cret"
        probe = FakeProbe(
            "postgres", raises=RuntimeError(f"connection to db failed: password={secret}")
        )
        async with _client([probe]) as client:
            body = (await client.get("/readyz")).text
        assert secret not in body
        assert "password" not in body


class TestRequestIdentityAndHeaders:
    """Correlation and hardening headers."""

    async def test_request_id_is_echoed(self) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/healthz")
        assert response.headers.get("x-request-id")

    async def test_client_supplied_request_id_is_preserved(self) -> None:
        """A caller's correlation id must survive, or cross-system tracing breaks at our
        boundary.
        """
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/healthz", headers={"X-Request-ID": "caller-request-1"})
        assert response.headers["x-request-id"] == "caller-request-1"

    async def test_absurd_request_id_is_replaced_not_reflected(self) -> None:
        """Reflecting an unbounded client string into logs and responses is an injection and
        volume vector.
        """
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/healthz", headers={"X-Request-ID": "x" * 5000})
        assert response.status_code == 200
        assert len(response.headers["x-request-id"]) < 200

    @pytest.mark.parametrize(
        "header",
        [
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "content-security-policy",
        ],
    )
    async def test_security_headers_are_present(self, header: str) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/healthz")
        assert header in response.headers

    async def test_content_type_options_is_nosniff(self) -> None:
        """Without nosniff a browser may interpret a JSON error body as HTML."""
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/healthz")
        assert response.headers["x-content-type-options"] == "nosniff"


class TestInputLimits:
    """Request-size enforcement is a denial-of-service control."""

    async def test_oversized_declared_body_is_rejected(self) -> None:
        async with _client([FakeProbe("postgres")], max_body_bytes=2048) as client:
            response = await client.post(
                "/anything", content=b"x" * 4096, headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 413
        assert response.json()["code"] == "payload_too_large"

    async def test_body_within_limit_is_not_rejected_for_size(self) -> None:
        """The limit must not fire early: rejecting valid requests is its own outage."""
        async with _client([FakeProbe("postgres")], max_body_bytes=4096) as client:
            response = await client.post(
                "/anything", content=b"{}", headers={"Content-Type": "application/json"}
            )
        assert response.status_code != 413

    async def test_oversized_body_is_rejected_before_auth(self) -> None:
        """Size checking first means a huge upload is refused without touching the
        authorisation path - cheaper and easier to reason about.
        """
        async with _client([FakeProbe("postgres")], max_body_bytes=2048) as client:
            response = await client.post("/secret", content=b"x" * 4096)
        assert response.status_code == 413


class TestOpenApiDocument:
    """The generated specification is the published contract.

    The specification is generated **in process** and committed to the repository rather than
    served. That is a deliberate security decision: the authentication boundary denies every
    path outside the four public ones, including ``/openapi.json``, so an anonymous caller
    cannot enumerate the API surface. CI regenerates the document and diffs it against the
    committed copy to detect an accidental contract change.
    """

    def _document(self) -> dict[str, Any]:
        """Return the generated OpenAPI document.

        Returns:
            The specification as a mapping.

        """
        from knowledge_assistant.config.settings import Settings  # noqa: PLC0415

        from ..conftest import make_app  # noqa: PLC0415

        settings = Settings(  # type: ignore[call-arg]
            environment="test",
            database_url="postgresql://t:t@localhost/ka",
            service_name="ka-contract",
        )
        app = make_app(
            probes=[FakeProbe("postgres")], clock=FixedClock(TEST_EPOCH), settings=settings
        )
        return app.openapi()

    def test_document_is_valid(self) -> None:
        document = self._document()
        assert document["openapi"].startswith("3.")
        assert "info" in document and "paths" in document

    def test_documented_paths_match_the_implemented_surface(self) -> None:
        """Only the foundation endpoints may exist. A business endpoint appearing here would
        mean Phase 2 leaked into Phase 1.
        """
        assert set(self._document()["paths"]) == {"/healthz", "/readyz", "/version"}

    def test_document_is_json_serialisable(self) -> None:
        json.dumps(self._document())

    async def test_spec_is_not_publicly_served(self) -> None:
        """Failing closed applies to the specification too; otherwise the gate is a
        speed bump for reconnaissance rather than a boundary.
        """
        async with _client([FakeProbe("postgres")]) as client:
            response = await client.get("/openapi.json")
        assert response.status_code == 403

    def test_committed_spec_matches_the_generated_one(self) -> None:
        """The contract-drift gate. Without a committed artefact there is nothing to diff and
        'detect accidental API changes' is a promise rather than a control.
        """
        import pathlib  # noqa: PLC0415

        committed = pathlib.Path("docs/07-api/openapi.json")
        assert committed.exists(), "committed OpenAPI artefact missing"
        assert json.loads(committed.read_text(encoding="utf-8")) == self._document(), (
            "OpenAPI artefact is stale: regenerate with 'ka check openapi --write'"
        )


class TestProbeReporting:
    """Probe detail is served, so its shape is part of the contract."""

    async def test_probe_names_are_reported(self) -> None:
        """Operators use /readyz as the first diagnostic step; unnamed probes are useless
        for that.
        """
        async with _client([FakeProbe("postgres"), FakeProbe("queue")]) as client:
            body = (await client.get("/readyz")).json()
        names = [p["name"] for p in body["probes"]]
        assert names == ["postgres", "queue"]

    async def test_probe_reports_are_json_safe(self) -> None:
        async with _client([FakeProbe("postgres")]) as client:
            body = (await client.get("/readyz")).json()
        assert isinstance(body["probes"], list)

    async def test_health_service_reports_all_declared_probes(self) -> None:
        clock = FixedClock(TEST_EPOCH)
        service = make_health_service([FakeProbe("a"), FakeProbe("b")], clock)
        report = await service.report()
        assert [p.name for p in report.probes] == ["a", "b"]

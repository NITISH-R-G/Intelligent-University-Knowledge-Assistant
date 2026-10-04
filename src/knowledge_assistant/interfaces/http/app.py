"""FastAPI application factory.

**Endpoints in Phase 1 are deliberately foundation-only.** Health, readiness, version and
metrics. No business endpoint exists, and the authentication middleware denies anything that is
not on the public list - so the absence of business endpoints is enforced rather than assumed.

**Liveness and readiness are different endpoints answering different questions:**

``/healthz`` - "is this process running?" It answers from memory and constant data. It never
touches the database. If it did, a database outage would make every instance look dead and an
orchestrator would restart the entire fleet - turning a recoverable dependency failure into a
restart loop that does not fix the database.

``/readyz`` - "should this instance receive traffic?" It runs the dependency probes and returns
503 when a critical probe fails. This is the only endpoint a load balancer should poll.

``/version`` - build identity. Answers "which build is this?" for support, and is the
foundation of the compatibility discipline: a client that cannot identify the server build
cannot report a bug reproducibly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from knowledge_assistant.application.health import HealthService
from knowledge_assistant.domain.clock import Clock
from knowledge_assistant.domain.health import ProbeStatus
from knowledge_assistant.interfaces.http.knowledge import (
    KnowledgeQueryPort,
    build_knowledge_router,
)
from knowledge_assistant.interfaces.http.middleware import (
    AccessLogMiddleware,
    AuthenticationBoundaryMiddleware,
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from knowledge_assistant.observability.metrics import Metrics

__all__ = [
    "create_app",
    "build_health_router",
    "build_knowledge_router",
    "build_meta_router",
    "SERVICE_VERSION",
    "register_middleware",
]

#: Service version. Kept in one place so ``/version`` and the metrics labels cannot disagree.
SERVICE_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    """Liveness response body."""

    status: str = Field(description="Always 'ok' if the process can answer at all.")
    service: str
    version: str


class ProbeView(BaseModel):
    """One probe's result, as exposed on readiness."""

    name: str
    status: str
    criticality: str
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness response body.

    Deliberately excludes anything that would help an attacker fingerprint the deployment:
    no version, no hostname, no dependency names beyond the probe's own name, which is already
    constrained by the health schema.
    """

    ready: bool = Field(description="Whether the instance should receive traffic.")
    degraded: bool = Field(description="True when an advisory dependency is unhealthy.")
    probes: list[ProbeView] = Field(default_factory=list)


def build_health_router(health: HealthService, *, service_name: str) -> APIRouter:
    """Build the liveness and readiness router.

    Args:
        health: Health use case.
        service_name: Value reported in the liveness body.

    Returns:
        An ``APIRouter`` exposing ``/healthz`` and ``/readyz``.

    """
    router = APIRouter(tags=["health"])

    @router.get(
        "/healthz",
        response_model=HealthResponse,
        summary="Liveness probe",
        description=(
            "Reports only that the process is running. Performs no dependency I/O by design, so "
            "a dependency outage cannot cause an orchestrator to restart every instance."
        ),
    )
    async def healthz() -> HealthResponse:
        """Return liveness.

        Returns:
            A 200 response while the process can serve requests.

        """
        return HealthResponse(status="ok", service=service_name, version=SERVICE_VERSION)

    @router.get(
        "/readyz",
        response_model=ReadinessResponse,
        summary="Readiness probe",
        description=(
            "Runs dependency probes and reports whether this instance should receive traffic. "
            "Returns 503 when a critical probe fails, which is the signal a load balancer needs."
        ),
    )
    async def readyz() -> Response:
        """Return readiness.

        Returns:
            200 with the report when ready, 503 with the same body when not. The body shape is
            identical in both cases so a client never has to branch on parsing.

        """
        report = await health.report()
        body = ReadinessResponse(
            ready=report.ready,
            degraded=report.degraded,
            probes=[
                ProbeView(
                    name=p.name,
                    status=p.status.value,
                    criticality=p.criticality.value,
                    detail=p.detail,
                )
                for p in report.probes
            ],
        )
        status_code = status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=status_code, content=body.model_dump())

    return router


def build_meta_router(*, settings_public: dict[str, Any]) -> APIRouter:
    """Build the version router.

    Args:
        settings_public: Allow-listed, publicly safe configuration summary.

    Returns:
        An ``APIRouter`` exposing ``/version``.

    """
    router = APIRouter(tags=["meta"])

    @router.get(
        "/version",
        summary="Build identity",
        description="Reports service name, version and environment. No secrets, no hostnames.",
    )
    async def version() -> dict[str, Any]:
        """Return build identity.

        Returns:
            Service name, version and the public configuration summary.

        """
        return {"version": SERVICE_VERSION, **settings_public}

    return router


def register_middleware(
    app: FastAPI,
    *,
    max_body_bytes: int,
    metrics: Metrics | None = None,
    require_authentication: bool = True,
) -> None:
    """Register the middleware stack in the documented execution order.

    Starlette builds the stack so that the **last registered middleware is the outermost**.
    To obtain the intended order (request id outermost, authentication innermost) the
    registration below therefore proceeds in reverse.

    Args:
        app: Application to instrument.
        max_body_bytes: Request body size limit.
        metrics: Optional metrics facade for the access log.
        require_authentication: Whether to install the failing-closed authentication gate. The
            flag defaults to true, and turning it off *removes a guardrail* rather than adding
            authentication - which is why settings validation refuses it outside local
            development. It exists so a local demo can reach a route the gate would otherwise
            deny, and so that doing so is an explicit, logged decision.

    """
    if require_authentication:
        app.add_middleware(AuthenticationBoundaryMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware, metrics=metrics)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    app.add_middleware(RequestIdMiddleware)


def create_app(
    *,
    health: HealthService,
    service_name: str,
    settings_public: dict[str, Any],
    clock: Clock,
    max_body_bytes: int = 1024 * 1024,
    metrics: Metrics | None = None,
    knowledge: KnowledgeQueryPort | None = None,
    require_authentication: bool = True,
) -> FastAPI:
    """Create the FastAPI application.

    The factory takes collaborators rather than constructing them, so tests can supply a
    ``FixedClock`` and fake probes without monkeypatching, and so the composition root is the
    only place that knows about concrete adapters.

    Args:
        health: Health use case backing the probes.
        service_name: Service name for logs and the liveness body.
        settings_public: Public configuration summary.
        clock: Injected clock, retained on ``app.state`` for middleware and tests.
        max_body_bytes: Request body size limit.
        metrics: Optional metrics facade.
        require_authentication: Whether to install the failing-closed authentication gate.
            Defaults to true.
        knowledge: End-to-end answering use case. When supplied, the knowledge query route is
            registered; when omitted the app serves only the foundation endpoints. Optional so
            the application layer stays importable by tests and health checks without a
            database, and so an app built for liveness never starts a connection pool.

    Returns:
        A configured ``FastAPI`` instance.

    """
    app = FastAPI(
        title="Intelligent University Knowledge Assistant",
        version=SERVICE_VERSION,
        summary="University knowledge assistant over an in-house retrieval pipeline.",
        description=(
            "Foundation endpoints plus POST /api/v1/knowledge/query, which answers from "
            "documents retrieved out of the local corpus and cites the passages it used. "
            "A question the corpus does not cover is refused rather than answered."
        ),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.state.clock = clock
    app.include_router(build_health_router(health, service_name=service_name))
    app.include_router(build_meta_router(settings_public=settings_public))
    if knowledge is not None:
        app.include_router(build_knowledge_router(knowledge))
    register_middleware(
        app,
        max_body_bytes=max_body_bytes,
        metrics=metrics,
        require_authentication=require_authentication,
    )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Return a pointer to the documentation.

        Returns:
            A short string describing where to look. Deliberately not a landing page.

        """
        return {
            "service": service_name,
            "docs": "/docs",
            "health": "/healthz",
            "ready": "/readyz",
            "knowledge": "/api/v1/knowledge/query",
        }

    return app


def probe_status_values() -> list[str]:
    """Return the probe status values, for documentation generation.

    Returns:
        Sorted status strings.

    """
    return sorted(s.value for s in ProbeStatus)

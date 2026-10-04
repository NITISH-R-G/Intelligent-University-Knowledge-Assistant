"""Composition root.

**The only module permitted to know every concrete adapter.** Everything else receives
collaborators. This is what makes the Phase 0 "replaceable infrastructure" claim testable: to
swap the database or the telemetry backend, one file changes.

Wiring is explicit rather than magical. There is no service locator and no decorator-based
injection, because both hide the dependency graph - and the dependency graph is the thing a
reader most needs to see. The cost is a slightly longer constructor; the benefit is that
``build_api_container`` reads as a complete answer to "what does this service touch?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI

from knowledge_assistant.application.health import HealthService
from knowledge_assistant.application.idempotency import IdempotentExecutor
from knowledge_assistant.application.jobs import ExecuteJobOnce, SubmitJob
from knowledge_assistant.application.ports import HealthProbePort
from knowledge_assistant.config.settings import Settings, load_settings
from knowledge_assistant.domain.clock import Clock, SystemClock
from knowledge_assistant.domain.health import ProbeCriticality, ProbeStatus
from knowledge_assistant.infrastructure.db.engine import Database, build_pool
from knowledge_assistant.infrastructure.db.idempotency import PgIdempotencyStore
from knowledge_assistant.infrastructure.db.jobs import PgJobRepository
from knowledge_assistant.infrastructure.db.probes import (
    DatabaseProbe,
    JobQueueProbe,
    StaticProbe,
)
from knowledge_assistant.interfaces.http.app import create_app
from knowledge_assistant.lifespan import make_lifespan
from knowledge_assistant.observability.logging import configure_logging, get_logger
from knowledge_assistant.observability.metrics import Metrics, build_meter
from knowledge_assistant.workers.handlers import build_registry
from knowledge_assistant.workers.runner import WorkerRunner

__all__ = ["ApiContainer", "WorkerContainer", "build_api_container", "build_worker_container"]


def _build_metrics(settings: Settings) -> Metrics:
    """Create the metrics facade.

    Args:
        settings: Validated settings.

    Returns:
        A metrics facade bound to the configured meter.

    """
    return Metrics(build_meter(settings.service_name), service=settings.service_name)


def _build_database(settings: Settings) -> Database:
    """Create the database handle with its pool.

    Args:
        settings: Validated settings.

    Returns:
        An unopened ``Database``.

    """
    pool = build_pool(
        settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        application_name=f"{settings.service_name}-api",
    )
    return Database(pool)


def _build_probes(
    settings: Settings,
    *,
    database: Database | None,
    jobs: Any | None,
    clock: Clock,
    component: str,
) -> tuple[HealthProbePort, ...]:
    """Build the probe set for a component.

    Args:
        settings: Validated settings.
        database: Database handle, or ``None`` when it cannot be built.
        jobs: Job repository, or ``None``.
        clock: Injected clock.
        component: ``"api"`` or ``"worker"``, which changes the queue probe's criticality.

    Returns:
        Ordered probes. Declared (not-yet-implemented) dependencies appear as advisory probes so
        their absence is visible rather than silent.

    """
    probes: list[HealthProbePort] = []
    if database is not None:
        probes.append(DatabaseProbe(database, clock))
    if jobs is not None:
        probes.append(JobQueueProbe(jobs, clock))
    probes.append(
        StaticProbe(
            name="object_store",
            status=ProbeStatus.DEGRADED,
            detail="not_wired_in_phase_1",
            criticality=ProbeCriticality.ADVISORY,
            clock=clock,
        )
    )
    probes.append(
        StaticProbe(
            name="model_runtime",
            status=ProbeStatus.DEGRADED,
            detail="not_wired_in_phase_1",
            criticality=ProbeCriticality.ADVISORY,
            clock=clock,
        )
    )
    del settings, component
    return tuple(probes)


@dataclass(slots=True)
class ApiContainer:
    """Everything the API process owns."""

    settings: Settings
    clock: Clock
    metrics: Metrics
    database: Database
    jobs: PgJobRepository
    health: HealthService
    submit_job: SubmitJob
    idempotency: IdempotentExecutor
    app: FastAPI

    async def aclose(self) -> None:
        """Close owned resources. Called on shutdown so no connection is leaked on deploy."""
        await self.database.close()


@dataclass(slots=True)
class WorkerContainer:
    """Everything the worker process owns."""

    settings: Settings
    clock: Clock
    metrics: Metrics
    database: Database
    jobs: PgJobRepository
    executor: ExecuteJobOnce
    runner: WorkerRunner
    health: HealthService

    async def aclose(self) -> None:
        """Close owned resources."""
        await self.database.close()


def build_api_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
) -> ApiContainer:
    """Wire the API process.

    Args:
        settings: Validated settings. Loaded from the environment when omitted.
        clock: Injected clock. Defaults to the system clock.

    Returns:
        A container whose ``app`` is ready to be served.

    Raises:
        pydantic.ValidationError: Propagated from settings loading. A misconfigured service
            must not start.

    """
    resolved_settings = settings or load_settings()
    configure_logging(
        level=resolved_settings.log_level,
        fmt=resolved_settings.log_format.value,
        service=resolved_settings.service_name,
        environment=resolved_settings.environment.value,
    )
    logger = get_logger("container")
    resolved_clock = clock or SystemClock()
    metrics = _build_metrics(resolved_settings)
    database = _build_database(resolved_settings)
    jobs = PgJobRepository(database)
    probes = _build_probes(
        resolved_settings, database=database, jobs=jobs, clock=resolved_clock, component="api"
    )
    health = HealthService(
        probes, clock=resolved_clock, timeout_seconds=resolved_settings.health_probe_timeout_seconds
    )
    submit = SubmitJob(
        jobs,
        clock=resolved_clock,
        max_attempts=resolved_settings.worker_max_attempts,
    )
    idempotent = IdempotentExecutor(
        PgIdempotencyStore(database),
        clock=resolved_clock,
        retention_seconds=resolved_settings.idempotency_retention_seconds,
    )
    app = create_app(
        health=health,
        service_name=resolved_settings.service_name,
        settings_public=resolved_settings.public_summary(),
        clock=resolved_clock,
        max_body_bytes=resolved_settings.max_request_body_bytes,
        metrics=metrics,
    )
    app.router.lifespan_context = cast("Any", make_lifespan(database))
    logger.info(
        "container.api.built",
        environment=resolved_settings.environment.value,
        probe_count=len(probes),
    )
    return ApiContainer(
        settings=resolved_settings,
        clock=resolved_clock,
        metrics=metrics,
        database=database,
        jobs=jobs,
        health=health,
        submit_job=submit,
        idempotency=idempotent,
        app=app,
    )


def build_worker_container(
    settings: Settings | None = None, *, clock: Clock | None = None
) -> WorkerContainer:
    """Wire the worker process.

    Args:
        settings: Validated settings, or loaded from the environment.
        clock: Injected clock.

    Returns:
        A container whose ``runner`` is ready to run.

    """
    resolved_settings = settings or load_settings()
    configure_logging(
        level=resolved_settings.log_level,
        fmt=resolved_settings.log_format.value,
        service=f"{resolved_settings.service_name}-worker",
        environment=resolved_settings.environment.value,
    )
    logger = get_logger("container")
    resolved_clock = clock or SystemClock()
    metrics = _build_metrics(resolved_settings)
    database = _build_database(resolved_settings)
    jobs = PgJobRepository(database)
    executor = ExecuteJobOnce(
        jobs,
        build_registry(),
        clock=resolved_clock,
        lease_seconds=resolved_settings.worker_lease_seconds,
        backoff_base_seconds=resolved_settings.worker_backoff_base_seconds,
        backoff_cap_seconds=resolved_settings.worker_backoff_cap_seconds,
    )
    runner = WorkerRunner(
        executor,
        clock=resolved_clock,
        poll_interval_seconds=resolved_settings.worker_poll_interval_seconds,
        metrics=metrics,
    )
    probes = _build_probes(
        resolved_settings, database=database, jobs=jobs, clock=resolved_clock, component="worker"
    )
    health = HealthService(
        probes, clock=resolved_clock, timeout_seconds=resolved_settings.health_probe_timeout_seconds
    )
    logger.info("container.worker.built", environment=resolved_settings.environment.value)
    return WorkerContainer(
        settings=resolved_settings,
        clock=resolved_clock,
        metrics=metrics,
        database=database,
        jobs=jobs,
        executor=executor,
        runner=runner,
        health=health,
    )

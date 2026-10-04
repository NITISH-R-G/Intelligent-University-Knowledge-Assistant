"""Benchmarks for the Phase 1 foundation.

Scope discipline, because this is the easiest thing to get wrong in a benchmark suite.

**Only code that exists is benchmarked.** Phase 1 has no retrieval, no generation, no
ingestion and no vector search, so this suite contains no search-latency or
answer-latency benchmark. Producing one would mean benchmarking a mock and publishing
the number as though it described the system.

**Only real code paths are measured.** The API benchmarks drive the actual FastAPI
application through an ASGI transport, so middleware, routing, request-ID handling and
the error layer are genuinely exercised. The database benchmark is a genuine BLOCKED
entry when no server is configured, not an in-memory double wearing a database
benchmark's name.

**Every result states its scope.** The API numbers exclude the socket and uvicorn. They
measure our code and are labelled ``in_process`` so they cannot be quoted as production
latency. See the protocol's "Known limitations".
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable

from benchmarks.framework import (
    Benchmark,
    BenchmarkResult,
    MeasurementScope,
    Status,
    blocked,
    run,
    run_async,
)
from knowledge_assistant.application.health import HealthService
from knowledge_assistant.config.settings import Settings, load_settings
from knowledge_assistant.domain.clock import FixedClock
from knowledge_assistant.domain.health import ProbeCriticality, ProbeStatus
from knowledge_assistant.domain.identifiers import validate_idempotency_key
from knowledge_assistant.domain.jobs import base_delay_seconds
from knowledge_assistant.infrastructure.db.probes import StaticProbe
from knowledge_assistant.interfaces.http.app import create_app

__all__ = ["collect_results", "BENCHMARK_DATABASE_URL_VARIABLE"]

#: Environment variable naming a PostgreSQL instance for the live-database benchmark.
#: Its absence is what makes that entry BLOCKED rather than quietly skipped.
BENCHMARK_DATABASE_URL_VARIABLE: str = "BENCHMARK_DATABASE_URL"

#: Fixed instant so nothing in a benchmark result depends on the wall clock.
_BENCHMARK_EPOCH = dt.datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt.UTC)

#: Settings used wherever a benchmark needs them. Constructed directly rather than read
#: from the environment so two runs on two machines measure the same thing - otherwise
#: the developer's shell silently becomes an unrecorded benchmark parameter.
_BENCHMARK_SETTINGS = Settings(
    environment="local",
    database_url="postgresql://benchmark:benchmark@localhost:5432/benchmark",
    service_name="benchmark",
)


def _build_health_service() -> HealthService:
    """Build a readiness service over non-I/O probes.

    Uses the real ``StaticProbe`` and the real ``HealthService``, so aggregation,
    concurrency and criticality rules are exercised. It deliberately does not use
    ``DatabaseProbe``: that needs a live server, and timing a connection refusal would
    measure this host's network stack rather than the application.

    Returns:
        A configured health service.

    """
    clock = FixedClock(_BENCHMARK_EPOCH)
    probes = [
        StaticProbe(
            name="object_store",
            status=ProbeStatus.DEGRADED,
            detail="not configured in Phase 1",
            criticality=ProbeCriticality.ADVISORY,
            clock=clock,
        ),
        StaticProbe(
            name="model_runtime",
            status=ProbeStatus.DEGRADED,
            detail="not configured in Phase 1",
            criticality=ProbeCriticality.ADVISORY,
            clock=clock,
        ),
    ]
    return HealthService(probes, clock=clock, timeout_seconds=2.0)


def _build_app() -> object:
    """Build the real FastAPI application over the benchmark settings.

    Returns:
        A configured ``FastAPI`` instance.

    """
    return create_app(
        health=_build_health_service(),
        service_name=_BENCHMARK_SETTINGS.service_name,
        settings_public=_BENCHMARK_SETTINGS.public_summary(),
        clock=FixedClock(_BENCHMARK_EPOCH),
    )


def benchmark_config_load() -> BenchmarkResult:
    """Measure loading the full settings object from a fixed environment.

    This is on the critical path of every process start: pydantic-settings parses the
    environment, every field is validated, the cross-field pool rule runs, and the
    classification assertion runs at import. Interpreter start-up is excluded.

    Returns:
        The result.

    """
    benchmark = Benchmark(
        "config.load_settings",
        units="milliseconds",
        notes="full environment parse and validation; excludes interpreter start-up",
    )

    # Set once, outside the timed region. The point is to measure Settings construction
    # from an environment, and polluting the developer's shell is not part of that.
    previous = {key: os.environ.get(key) for key in _CONFIG_ENVIRONMENT}
    os.environ.update(_CONFIG_ENVIRONMENT)
    try:
        return run(benchmark, load_settings)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


#: The environment a configuration load is measured against. Explicit rather than
#: inherited so the benchmark input is visible in the source.
_CONFIG_ENVIRONMENT: dict[str, str] = {
    "KA_ENVIRONMENT": "local",
    "KA_DATABASE_URL": "postgresql://benchmark:benchmark@localhost:5432/benchmark",
    "KA_SERVICE_NAME": "benchmark",
    "KA_PORT": "8080",
}


def benchmark_idempotency_key_validation() -> BenchmarkResult:
    """Measure validation of a well-formed idempotency key.

    Idempotency keys are validated before anything else happens on the request path. This
    measures the accepted case; the rejection path is the one an attacker drives and is
    covered by the unit tests.

    Returns:
        The result.

    """
    benchmark = Benchmark(
        "domain.validate_idempotency_key",
        units="milliseconds",
        notes="accepted-key path only; rejection path is covered by unit tests",
    )
    key = "idem-benchmark-key-000001"
    return run(benchmark, lambda: validate_idempotency_key(key))


def benchmark_backoff_computation() -> BenchmarkResult:
    """Measure the retry backoff calculation.

    Pure arithmetic on the hot path of every failed job attempt. Included because it is
    trivially optimisable and therefore easy to regress without noticing.

    Returns:
        The result.

    """
    benchmark = Benchmark(
        "domain.base_delay_seconds",
        units="milliseconds",
        notes="pure arithmetic, no I/O; measures exponentiation and clamping",
    )
    return run(benchmark, lambda: base_delay_seconds(4, base_seconds=1.0, cap_seconds=300.0))


async def _health_report_once() -> None:
    """Run one readiness aggregation."""
    await _build_health_service().report()


def benchmark_health_aggregation() -> BenchmarkResult:
    """Measure readiness aggregation over non-I/O probes.

    Real ``HealthService``: concurrent execution, per-probe timeout and criticality rules
    are all exercised. The probes return fixed values, so this measures our aggregation
    cost and nothing about any dependency.

    Returns:
        The result.

    """
    benchmark = Benchmark(
        "application.health_report_static_probes",
        units="milliseconds",
        notes="StaticProbe returns a constant; excludes any real dependency call",
    )
    return run_async(benchmark, _health_report_once, iterations=500)


async def _api_request(app: object, path: str, expected_status: int) -> None:
    """Issue one in-process HTTP request against the real application.

    Args:
        app: The application to call.
        path: Path to request.
        expected_status: Status the path is expected to return. Asserted rather than
            assumed: a benchmark that silently started measuring a 500 would publish a
            fast number for a broken endpoint.

    Raises:
        AssertionError: If the response status differs from ``expected_status``.

    """
    import httpx  # noqa: PLC0415

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
        response = await client.get(path)
    assert response.status_code == expected_status, (
        f"{path} returned {response.status_code}, expected {expected_status}"
    )


def _silence_structlog_output() -> None:
    """Keep log *processing* in the measurement while discarding the console write.

    The application logs every request. Writing 500 lines per benchmark to the terminal
    makes the report unusable and measures a terminal, which is not what a deployed
    service does - it writes to a pipe. ``ReturnLoggerFactory`` lets the whole structlog
    processor chain run (context merge, redaction, timestamping, rendering) and throws
    away only the final write, so the cost of producing a log line stays in the number.

    The alternative - filtering by level - would skip the processor chain entirely and
    understate the cost. See the protocol's "Known limitations".

    """
    import structlog  # noqa: PLC0415

    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())


def _api_benchmark(
    name: str, path: str, *, notes: str, expected_status: int
) -> Callable[[], BenchmarkResult]:
    """Build an API benchmark runner.

    Args:
        name: Benchmark name.
        path: Path to request.
        notes: Caveat carried into the result.
        expected_status: Status the path must return for the measurement to be valid.

    Returns:
        A zero-argument runner producing one result.

    """

    def runner() -> BenchmarkResult:
        """Measure one API path.

        Returns:
            The result.

        """
        benchmark = Benchmark(
            name,
            units="milliseconds",
            scope=MeasurementScope.IN_PROCESS,
            notes=notes,
        )
        # The app is built once, outside the timed region: measuring construction on every
        # iteration would report settings validation and DI wiring instead of the request.
        _silence_structlog_output()
        app = _build_app()
        return run_async(
            benchmark,
            lambda: _api_request(app, path, expected_status),
            iterations=500,
        )

    return runner


benchmark_api_version = _api_benchmark(
    "api.request_version",
    "/version",
    notes="ASGI in-process; excludes socket, TLS and uvicorn. NOT a production latency",
    expected_status=200,
)
benchmark_api_healthz = _api_benchmark(
    "api.request_healthz",
    "/healthz",
    notes="ASGI in-process; excludes socket, TLS and uvicorn. NOT a production latency",
    expected_status=200,
)
benchmark_api_readyz = _api_benchmark(
    "api.request_readyz",
    "/readyz",
    notes="ASGI in-process; excludes socket, TLS and uvicorn. NOT a production latency",
    expected_status=200,
)
benchmark_api_denied_path = _api_benchmark(
    "api.request_denied_path",
    "/not-public",
    notes="403 from the fail-closed auth boundary; ASGI in-process",
    expected_status=403,
)


def benchmark_live_database_connect() -> BenchmarkResult:
    """Measure establishing a real PostgreSQL connection.

    Requires ``BENCHMARK_DATABASE_URL``. Without it this reports BLOCKED with a reason
    rather than timing a connection refusal - the time to receive a refusal is a property
    of this host's network stack, not of the application.

    Returns:
        The result, or a BLOCKED result.

    """
    benchmark = Benchmark(
        "db.connect",
        units="milliseconds",
        scope=MeasurementScope.LIVE_DATABASE,
        notes="real PostgreSQL connection establishment",
    )
    dsn = os.environ.get(BENCHMARK_DATABASE_URL_VARIABLE)
    if not dsn:
        return blocked(
            benchmark,
            f"{BENCHMARK_DATABASE_URL_VARIABLE} is not set; "
            "no live PostgreSQL available to measure against",
        )

    async def _connect_once() -> None:
        """Open and close one connection.

        Raises:
            psycopg.Error: Propagated from the driver and surfaced as an ERROR result.

        """
        import psycopg  # noqa: PLC0415

        connection = await psycopg.AsyncConnection.connect(dsn, connect_timeout=5)
        await connection.close()

    return run_async(benchmark, _connect_once, iterations=25, warmup_iterations=3)


def collect_results(
    *,
    include_database: bool = True,
    only: str | None = None,
) -> list[BenchmarkResult]:
    """Run the suite and return results in a stable order.

    Args:
        include_database: Whether to attempt the live-database benchmark. It reports
            BLOCKED rather than failing when no instance is configured.
        only: Substring filter on the runner name, for running a single benchmark.

    Returns:
        Results, in suite order.

    """
    runners: list[tuple[str, Callable[[], BenchmarkResult]]] = [
        ("config", benchmark_config_load),
        ("idempotency", benchmark_idempotency_key_validation),
        ("backoff", benchmark_backoff_computation),
        ("health", benchmark_health_aggregation),
        ("version", benchmark_api_version),
        ("healthz", benchmark_api_healthz),
        ("readyz", benchmark_api_readyz),
        ("denied", benchmark_api_denied_path),
    ]
    if include_database:
        runners.append(("database", benchmark_live_database_connect))

    return [runner() for label, runner in runners if only is None or only in label]


def has_errors(results: list[BenchmarkResult]) -> bool:
    """Return whether any benchmark failed to execute.

    A convenience predicate so the runner can pick an exit code without repeating the
    enum comparison. BLOCKED is deliberately not an error: an unavailable database is an
    environment fact, not a defect in the code under test.

    Args:
        results: Results to inspect.

    Returns:
        ``True`` when at least one benchmark returned ``Status.ERROR``.

    """
    return any(result.status is Status.ERROR for result in results)

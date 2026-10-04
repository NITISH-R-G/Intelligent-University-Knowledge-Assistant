"""Metrics.

OpenTelemetry is used rather than a Prometheus client library directly, because OTel is the
one choice where leaving is a configuration change rather than a rewrite (ADR-006). The
Prometheus exporter is the Phase 1 sink; the same instruments export to any other OTel backend
without code changes.

**Cardinality is enforced by construction.** Every instrument is created here with a fixed
label *set*. There is no API for adding an arbitrary label. This is the single most effective
guard against the most common self-inflicted observability outage: a label containing a user id,
request id or raw URL multiplies the series count and can make a Prometheus deployment
unusable.

Allowed label dimensions across all Phase 1 instruments are limited to: ``service``,
``component``, ``method``, ``route`` (a template), ``status_class``, ``outcome``, ``job_type``
and ``state``. Anything finer belongs in a trace or a log, where cardinality is cheap.

**Percentiles are computed by the backend, not here.** Histograms emit bucket counts at fixed
boundaries; the bucket edges are chosen to bracket the Phase 0 latency budget so that p95 and
p99 are answerable. The edges are a design choice, documented, and revisited once real
latency data exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter

__all__ = [
    "MetricNames",
    "Metrics",
    "build_meter",
    "LATENCY_BUCKETS_SECONDS",
]

#: Histogram bucket edges in seconds. Bracket the Phase 0 targets (search p95 0.25 s,
#: extractive answer p95 0.4 s, generated answer p95 8 s) so percentiles are answerable.
#: DESIGN TARGET - not derived from measurement. See docs/00-overview/MEASUREMENT_TERMINOLOGY.md.
LATENCY_BUCKETS_SECONDS: Final[tuple[float, ...]] = (
    0.005,
    0.010,
    0.025,
    0.050,
    0.100,
    0.250,
    0.500,
    1.000,
    2.500,
    5.000,
    10.000,
    30.000,
)


class MetricNames:
    """Canonical metric names. Declared as constants so a typo is an ``AttributeError``."""

    HTTP_REQUESTS: Final[str] = "ka.http.requests"
    HTTP_ERRORS: Final[str] = "ka.http.errors"
    HTTP_LATENCY: Final[str] = "ka.http.request.duration"
    HTTP_INFLIGHT: Final[str] = "ka.http.inflight"
    DB_QUERY_DURATION: Final[str] = "ka.db.query.duration"
    DB_CONNECTIONS: Final[str] = "ka.db.connections"
    DB_HEALTH: Final[str] = "ka.db.health"
    WORKER_JOBS_CLAIMED: Final[str] = "ka.worker.jobs.claimed"
    WORKER_JOB_OUTCOMES: Final[str] = "ka.worker.job.outcomes"
    WORKER_JOB_DURATION: Final[str] = "ka.worker.job.duration"
    WORKER_QUEUE_DEPTH: Final[str] = "ka.worker.queue.depth"
    WORKER_DEAD_LETTER_DEPTH: Final[str] = "ka.worker.dead_letter.depth"
    WORKER_OLDEST_PENDING_AGE: Final[str] = "ka.worker.oldest_pending.age"
    PROCESS_RSS: Final[str] = "ka.process.memory.rss"
    PROCESS_CPU: Final[str] = "ka.process.cpu"
    PROCESS_UPTIME: Final[str] = "ka.process.uptime"


@dataclass(frozen=True, slots=True)
class InstrumentMeta:
    """Documentation carried alongside an instrument.

    Every Phase 1 metric must answer: what does it measure, who reads it, and what does an alert
    on it imply. Recording that next to the instrument makes the metric catalogue a
    by-construction artefact of the code rather than a document that drifts.
    """

    name: str
    type: str
    labels: tuple[str, ...]
    purpose: str
    owner: str
    alert_implication: str


#: The metric catalogue. Exported into docs/10-observability/METRICS.md and asserted against
#: the live instruments by tests/unit/test_metrics_catalogue.py, so the documentation cannot
#: drift from the implementation.
METRIC_CATALOGUE: Final[tuple[InstrumentMeta, ...]] = (
    InstrumentMeta(
        MetricNames.HTTP_REQUESTS,
        "counter",
        ("method", "route", "status_class"),
        "Total HTTP requests handled, by route template and status class.",
        "api team",
        "None. This is a denominator, never paged on its own.",
    ),
    InstrumentMeta(
        MetricNames.HTTP_ERRORS,
        "counter",
        ("method", "route", "status_class"),
        "HTTP responses with status >= 500. Feeds the availability SLO numerator.",
        "api team",
        "Page when the SLO burn rate exceeds the fast threshold.",
    ),
    InstrumentMeta(
        MetricNames.HTTP_LATENCY,
        "histogram",
        ("method", "route"),
        "End-to-end request duration. Phase 0 targets are p95 < 250 ms (search) "
        "and < 8 s (generated).",
        "api team",
        "Ticket at sustained p95 over target; page only on SLO burn.",
    ),
    InstrumentMeta(
        MetricNames.HTTP_INFLIGHT,
        "updowncounter",
        (),
        "Concurrent in-flight requests. Rising without completion means a saturation problem.",
        "api team",
        "Sustained rise with flat latency suggests a leak; with rising latency, a bottleneck.",
    ),
    InstrumentMeta(
        MetricNames.DB_QUERY_DURATION,
        "histogram",
        ("operation", "outcome"),
        "Duration of individual database operations.",
        "api team",
        "p95 over 100 ms means the statement timeout (5 s) is the only remaining backstop.",
    ),
    InstrumentMeta(
        MetricNames.DB_CONNECTIONS,
        "updowncounter",
        ("state",),
        "Connections in the pool by state: idle, in_use, or wait. "
        "Pool wait is a leading indicator.",
        "api team",
        "Sustained non-zero wait precedes connection exhaustion, which is an outage.",
    ),
    InstrumentMeta(
        MetricNames.DB_HEALTH,
        "gauge",
        (),
        "1 if the database answered a recent probe, 0 otherwise.",
        "api team",
        "Page on 0 for longer than the readiness probe interval: the database is down.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_JOBS_CLAIMED,
        "counter",
        ("job_type",),
        "Jobs claimed for execution.",
        "worker",
        "None. Denominator for outcomes.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_JOB_OUTCOMES,
        "counter",
        ("job_type", "outcome"),
        "Terminal job outcomes: succeeded, retry_scheduled, dead_lettered.",
        "worker",
        "Any dead_lettered is an actionable alert; a rising retry rate is a dependency problem.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_JOB_DURATION,
        "histogram",
        ("job_type",),
        "Job execution duration excluding queue wait.",
        "worker",
        "p95 over target means the worker pool is the bottleneck, not the database.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_QUEUE_DEPTH,
        "gauge",
        ("state",),
        "Jobs waiting by state.",
        "worker",
        "Sustained PENDING growth means ingestion cannot keep up.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_DEAD_LETTER_DEPTH,
        "gauge",
        (),
        "Dead-lettered jobs awaiting attention.",
        "worker",
        "Any growth pages. A dead-lettered job is work that will never complete on its own.",
    ),
    InstrumentMeta(
        MetricNames.WORKER_OLDEST_PENDING_AGE,
        "gauge",
        (),
        "Age of the oldest pending job in seconds. This is the freshness SLO signal (SLI-07).",
        "worker",
        "Warn at 120 s, page at 600 s: ingestion is visibly behind.",
    ),
    InstrumentMeta(
        MetricNames.PROCESS_RSS,
        "updowncounter",
        (),
        "Resident set size in bytes. Bounded memory is what makes horizontal scaling safe.",
        "api team",
        "Approaching the container memory limit predicts an OOM kill.",
    ),
    InstrumentMeta(
        MetricNames.PROCESS_CPU,
        "counter",
        (),
        "Process CPU time in seconds.",
        "api team",
        "Saturation (> 90% of limit) with rising latency means compute-bound.",
    ),
    InstrumentMeta(
        MetricNames.PROCESS_UPTIME,
        "gauge",
        (),
        "Process uptime in seconds. Uptime resets on restart, which is the "
        "signature of a crash loop.",
        "api team",
        "Repeated resets indicate a crash loop; investigate before scaling out.",
    ),
)


class Metrics:
    """Typed facade over an OpenTelemetry meter.

    The facade exists so that call sites cannot construct instruments with ad-hoc labels, and
    so that tests can assert on instrument *names* without importing OTel.
    """

    __slots__ = (
        "_meter",
        "_service",
        "http_requests",
        "http_errors",
        "http_latency",
        "http_inflight",
        "db_query_duration",
        "db_connections",
        "db_health",
        "worker_jobs_claimed",
        "worker_job_outcomes",
        "worker_job_duration",
        "worker_queue_depth",
        "worker_dead_letter_depth",
        "worker_oldest_pending_age",
    )

    def __init__(self, meter: Meter, *, service: str) -> None:
        """Create the instrument set.

        Args:
            meter: OpenTelemetry meter to create instruments from.
            service: Value of the ``service`` label. Adding ``service`` to every instrument
                rather than using one meter per service keeps the catalogue uniform.

        """
        self._meter = meter
        self._service = service
        self.http_requests: Counter = meter.create_counter(
            MetricNames.HTTP_REQUESTS, unit="{request}", description="HTTP requests handled"
        )
        self.http_errors: Counter = meter.create_counter(
            MetricNames.HTTP_ERRORS, unit="{request}", description="HTTP 5xx responses"
        )
        self.http_latency: Histogram = meter.create_histogram(
            MetricNames.HTTP_LATENCY, unit="s", description="Request duration"
        )
        self.http_inflight: UpDownCounter = meter.create_up_down_counter(
            MetricNames.HTTP_INFLIGHT, unit="{request}", description="In-flight requests"
        )
        self.db_query_duration: Histogram = meter.create_histogram(
            MetricNames.DB_QUERY_DURATION, unit="s", description="Database operation duration"
        )
        self.db_connections: UpDownCounter = meter.create_up_down_counter(
            MetricNames.DB_CONNECTIONS, unit="{connection}", description="Pool connections by state"
        )
        self.db_health: UpDownCounter = meter.create_up_down_counter(
            MetricNames.DB_HEALTH, unit="1", description="Database health (1 = up)"
        )
        self.worker_jobs_claimed: Counter = meter.create_counter(
            MetricNames.WORKER_JOBS_CLAIMED, unit="{job}", description="Jobs claimed"
        )
        self.worker_job_outcomes: Counter = meter.create_counter(
            MetricNames.WORKER_JOB_OUTCOMES, unit="{job}", description="Terminal job outcomes"
        )
        self.worker_job_duration: Histogram = meter.create_histogram(
            MetricNames.WORKER_JOB_DURATION, unit="s", description="Job execution duration"
        )
        self.worker_queue_depth: UpDownCounter = meter.create_up_down_counter(
            MetricNames.WORKER_QUEUE_DEPTH, unit="{job}", description="Queue depth by state"
        )
        self.worker_dead_letter_depth: UpDownCounter = meter.create_up_down_counter(
            MetricNames.WORKER_DEAD_LETTER_DEPTH, unit="{job}", description="Dead-lettered jobs"
        )
        self.worker_oldest_pending_age: UpDownCounter = meter.create_up_down_counter(
            MetricNames.WORKER_OLDEST_PENDING_AGE,
            unit="s",
            description="Age of oldest pending job",
        )

    @property
    def service(self) -> str:
        """Return the service label applied to every instrument."""
        return self._service

    @property
    def common_labels(self) -> tuple[tuple[str, str], ...]:
        """Return the fixed ``(service, component)`` pair every metric carries."""
        return (("service", self._service),)

    @property
    def catalogue(self) -> Sequence[InstrumentMeta]:
        """Return the documented metric catalogue."""
        return METRIC_CATALOGUE


def build_meter(service: str) -> Meter:
    """Return a meter scoped to the named service.

    Args:
        service: Service name. Used as the instrumentation-scope name so that instruments
            from the API and the worker are distinguishable in an exporter even when both
            processes write to the same backend.

    Returns:
        An OpenTelemetry meter.

    """
    provider = metrics.get_meter_provider()
    return provider.get_meter(f"knowledge_assistant.{service}", "0.1.0")

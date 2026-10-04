"""Health domain model.

The Phase 0 reliability model requires liveness and readiness to mean *different things*, and a
procurement-shaped health check is the classic way that distinction is lost. This module makes the
distinction a data model rather than a convention:

* A **critical** probe failing means "this process cannot serve traffic" -> readiness fails.
* An **advisory** probe failing means "degraded" -> readiness passes with a ``degraded`` flag.

The worker is the motivating case. If the queue backing store is unavailable, the *API* is still
correctly answering health queries, and pulling the API out of rotation because a worker cannot
reach the database would turn a partial outage into a total one. Conversely a dependency the API
genuinely needs must be critical.

Keeping this as pure aggregation logic means the aggregation rules are unit-tested without a
database, which is the only way to be confident about them.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "ProbeStatus",
    "ProbeCriticality",
    "ProbeResult",
    "HealthReport",
    "aggregate",
    "safe_probe_detail",
]


class ProbeStatus(enum.StrEnum):
    """Outcome of one probe."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class ProbeCriticality(enum.StrEnum):
    """Whether a probe gates readiness.

    ``CRITICAL`` fails readiness. ``ADVISORY`` does not; it only degrades the report. The
    distinction is a product decision per probe, made where the probe is declared, not
    inferred.
    """

    CRITICAL = "critical"
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of a single dependency probe.

    Attributes:
        name: Stable probe identifier, e.g. ``postgres``. Appears in metrics labels, so it
            must be low-cardinality - a probe name is a fixed enum in practice, never a
            dynamic string.
        status: Outcome.
        criticality: Whether this probe gates readiness.
        detail: Operator-facing detail. Never contains connection strings, credentials or
            hostnames in production; the infrastructure adapters are responsible for keeping
            it to a dependency name and a driver error code.
        latency_ms: Probe duration, used for the readiness latency budget.
        checked_at: Time the probe ran.
    """

    name: str
    status: ProbeStatus
    criticality: ProbeCriticality
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregated health of one process.

    Attributes:
        ready: Whether the process should receive traffic. ``False`` if any CRITICAL probe
            failed.
        degraded: Whether any ADVISORY probe failed or reported DEGRADED. Readiness can be
            ``True`` while ``degraded`` is ``True``; that combination is the entire point of
            separating the two.
        probes: Individual results, for diagnosis.
        generated_at: Time of aggregation.
    """

    ready: bool
    degraded: bool
    probes: Sequence[ProbeResult]
    generated_at: dt.datetime


def aggregate(probes: Iterable[ProbeResult], *, now: dt.datetime) -> HealthReport:
    """Aggregate probe results into a readiness decision.

    The rules, in full - there are exactly three:

    1. Any CRITICAL probe with status FAILED or DEGRADED means ``ready = False``.
       A degraded *critical* dependency is treated as not-ready because serving traffic onto
       a degraded dependency converts a partial failure into user-visible errors.
    2. Any ADVISORY probe not OK means ``degraded = True`` but leaves ``ready`` unchanged.
    3. An empty probe set yields ``ready = True``. Callers decide whether an empty set is
       acceptable; a service with no declared dependencies legitimately has none.

    Args:
        probes: Probe results, in declaration order.
        now: Aggregation time from the injected clock.

    Returns:
        The aggregated report.
    """
    ordered: Sequence[ProbeResult] = tuple(probes)
    ready = True
    degraded = False
    for probe in ordered:
        healthy = probe.status is ProbeStatus.OK
        if not healthy:
            if probe.criticality is ProbeCriticality.CRITICAL:
                ready = False
            else:
                degraded = True
    return HealthReport(ready=ready, degraded=degraded, probes=ordered, generated_at=now)


def safe_probe_detail(exc: BaseException) -> str:
    """Return a safe, fixed-vocabulary description of a probe exception.

    Only the exception's **class name** is used. The message is never included, because
    database driver exceptions routinely embed connection strings, hostnames, role names and
    occasionally the failed SQL - and probe output is served on an unauthenticated readiness
    endpoint. A readiness endpoint is not the place to leak a DSN.

    Keeping this pure means it is testable with arbitrary exception objects and carries no
    driver knowledge.

    Args:
        exc: Exception raised by a probe.

    Returns:
        A token of the form ``failed:<exception_class_lowercased>``.
    """
    name = type(exc).__name__
    safe = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_").lower()
    return f"failed:{safe or 'unknown'}"
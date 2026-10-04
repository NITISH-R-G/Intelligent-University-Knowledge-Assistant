"""Health use case.

Runs every declared probe concurrently under one timeout, then delegates the readiness decision
to the pure aggregation rules in :mod:`knowledge_assistant.domain.health`.

Three behaviours here are deliberate and each exists because of a specific failure:

**Probes run concurrently.** Sequential probes make total readiness latency the sum of every
dependency's latency. The slowest dependency should set the budget, not the sum.

**There is one timeout, and it is shorter than the caller's.** A probe that hangs must not hold
the readiness endpoint open; the readiness endpoint's own client (a load balancer) has a timeout
of its own and will remove the instance first. Failing at a bounded time with
``ProbeStatus.FAILED`` produces a truthful "not ready", whereas a slow response produces an
unexplained timeout to the caller and an unmeasured latency to us.

**A probe that raises is a failed probe, not a crashed health endpoint.** Exception-to-probe
translation happens here so that no probe implementation can take down the endpoint that
reports on it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from knowledge_assistant.application.ports import HealthProbePort
from knowledge_assistant.domain.clock import Clock
from knowledge_assistant.domain.health import (
    HealthReport,
    ProbeCriticality,
    ProbeResult,
    ProbeStatus,
    aggregate,
)
from knowledge_assistant.domain.health import safe_probe_detail

__all__ = ["HealthService", "DEFAULT_PROBE_TIMEOUT_SECONDS"]

#: Bound on the whole probe set. Chosen to sit inside a typical load-balancer probe timeout
#: (commonly 5 s) with margin. This is a DESIGN TARGET, not a measurement - see
#: docs/00-overview/MEASUREMENT_TERMINOLOGY.md.
DEFAULT_PROBE_TIMEOUT_SECONDS: float = 2.0


class HealthService:
    """Aggregates dependency probes into a readiness report."""

    __slots__ = ("_probes", "_clock", "_timeout_seconds")

    def __init__(
        self,
        probes: Sequence[HealthProbePort],
        *,
        clock: Clock,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """Build the service.

        Args:
            probes: Probes to run. Order is preserved in the report for stable output.
            clock: Injected time source.
            timeout_seconds: Upper bound on the whole probe set.
        """
        self._probes = tuple(probes)
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    @property
    def probe_names(self) -> tuple[str, ...]:
        """Return the declared probe names, in order."""
        return tuple(p.name for p in self._probes)

    async def report(self) -> HealthReport:
        """Run all probes concurrently and return the aggregated report.

        Returns:
            A ``HealthReport``. This method does not raise: an unhealthy system is a
            reportable state, not an exceptional one.
        """
        started = time.perf_counter()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(self._run_one(p) for p in self._probes)),
                timeout=self._timeout_seconds,
            )
            ordered: tuple[ProbeResult, ...] = tuple(results)
        except TimeoutError:
            # Any probe that did not finish is reported FAILED. We cannot know which, so we
            # mark all: reporting "ready" would be a lie, and reporting only the slow ones
            # would require per-probe timeouts that add their own failure modes.
            ordered = tuple(
                ProbeResult(
                    name=p.name,
                    status=ProbeStatus.FAILED,
                    criticality=p.criticality,
                    detail=f"probe set exceeded {self._timeout_seconds}s budget",
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    checked_at=self._clock.now(),
                )
                for p in self._probes
            )
        return aggregate(ordered, now=self._clock.now())

    async def _run_one(self, probe: HealthProbePort) -> ProbeResult:
        """Run a single probe, converting any exception or slowness into a result.

        Args:
            probe: Probe to run.

        Returns:
            The probe result. Never raises.
        """
        started = time.perf_counter()
        try:
            result = await probe.check()
        except Exception as exc:  # noqa: BLE001 - deliberate: a probe must not crash health
            return ProbeResult(
                name=probe.name,
                status=ProbeStatus.FAILED,
                criticality=ProbeCriticality.CRITICAL,
                detail=safe_probe_detail(exc),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                checked_at=self._clock.now(),
            )
        return ProbeResult(
            name=result.name,
            status=result.status,
            criticality=result.criticality,
            detail=result.detail,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            checked_at=self._clock.now(),
        )
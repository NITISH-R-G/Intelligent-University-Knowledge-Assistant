"""Health probes backed by real dependencies.

**Why the API's readiness probe on the database is CRITICAL and the queue probe is ADVISORY.**
This is the concrete case the Phase 0 reliability model flagged: the API can serve health and
metadata queries without the job queue. Making the queue probe critical would take the API out
of rotation because a *worker-side* dependency is down, converting a partial outage into a
total one. Making the database probe advisory would report an instance ready while every
request it serves fails.

**No probe reports a DSN, hostname or query text.** ``safe_probe_detail`` is applied inside the
application use case, and probes return only a status plus a short detail token. The readiness
endpoint is unauthenticated by design; it is the wrong place to disclose infrastructure.

**Probes are cheap by construction.** ``SELECT 1`` for the database; an indexed aggregate for
the queue. A probe that costs a table scan becomes a load problem under load, which is precisely
when the answer matters.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from knowledge_assistant.application.ports import HealthProbePort
from knowledge_assistant.domain.health import ProbeCriticality, ProbeResult, ProbeStatus

__all__ = ["DatabaseProbe", "JobQueueProbe", "StaticProbe"]


class DatabaseProbe:
    """Verifies that the database answers a trivial query.

    CRITICAL: without the database the API cannot serve any request that touches state.
    """

    __slots__ = ("_database", "_clock")

    def __init__(self, database: Any, clock: Any) -> None:
        """Bind a database handle and clock.

        Args:
            database: Object exposing ``acquire()``.
            clock: Injected time source.
        """
        self._database = database
        self._clock = clock

    @property
    def name(self) -> str:
        """Return the probe name used as a metric label."""
        return "postgres"

    @property
    def criticality(self) -> ProbeCriticality:
        """Return CRITICAL: the API cannot function without it."""
        return ProbeCriticality.CRITICAL

    async def check(self) -> ProbeResult:
        """Run ``SELECT 1`` and report the outcome.

        Returns:
            A probe result. Exceptions are allowed to propagate; the use case converts them,
            which keeps this adapter free of error-classification logic.
        """
        async with self._database.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.OK,
            criticality=self.criticality,
            detail="ok",
            checked_at=self._clock.now(),
        )


class JobQueueProbe:
    """Reports queue backlog and dead-letter depth.

    ADVISORY for API readiness: a backlog is an ingestion problem, not an API problem. The same
    probe is consumed by the *worker's* own readiness endpoint, where it is CRITICAL, because a
    worker that cannot see its queue should not be considered healthy.
    """

    __slots__ = ("_jobs", "_clock")

    def __init__(self, jobs: Any, clock: Any) -> None:
        """Bind a job repository and clock.

        Args:
            jobs: Job repository.
            clock: Injected time source.
        """
        self._jobs = jobs
        self._clock = clock

    @property
    def name(self) -> str:
        """Return the probe name."""
        return "job_queue"

    @property
    def criticality(self) -> ProbeCriticality:
        """Return ADVISORY in the API context."""
        return ProbeCriticality.ADVISORY

    async def check(self) -> ProbeResult:
        """Report DEGRADED when the dead-letter queue is non-empty.

        Returns:
            A probe result. A non-empty dead-letter queue means work that will never complete
            unattended, which is a real degradation even though the queue is reachable.
        """
        now: dt.datetime = self._clock.now()
        dead_letters = await self._jobs.dead_letter_count()
        status = ProbeStatus.DEGRADED if dead_letters > 0 else ProbeStatus.OK
        detail = f"dead_lettered={dead_letters}"
        return ProbeResult(
            name=self.name,
            status=status,
            criticality=self.criticality,
            detail=detail,
            checked_at=now,
        )


class StaticProbe:
    """A probe with a fixed status.

    Used for declared-but-not-yet-implemented dependencies so that their absence is visible on
    the readiness endpoint as an explicit advisory rather than as silence. Phase 1 uses it to
    state truthfully that object storage and the model runtime are not yet wired.
    """

    __slots__ = ("_name", "_status", "_detail", "_criticality", "_clock")

    def __init__(
        self,
        *,
        name: str,
        status: ProbeStatus,
        detail: str,
        criticality: ProbeCriticality,
        clock: Any,
    ) -> None:
        """Build a fixed-status probe.

        Args:
            name: Probe name.
            status: Fixed status.
            detail: Fixed detail string. Must not disclose infrastructure internals.
            criticality: Whether the probe gates readiness.
            clock: Injected time source.
        """
        self._name = name
        self._status = status
        self._detail = detail
        self._criticality = criticality
        self._clock = clock

    @property
    def name(self) -> str:
        """Return the probe name."""
        return self._name

    @property
    def criticality(self) -> ProbeCriticality:
        """Return the declared criticality."""
        return self._criticality

    async def check(self) -> ProbeResult:
        """Return the fixed result."""
        return ProbeResult(
            name=self._name,
            status=self._status,
            criticality=self._criticality,
            detail=self._detail,
            checked_at=self._clock.now(),
        )
"""Ports required by Phase 1 use cases.

Phase 0 proposed eleven ports. The Phase 1 review (docs/03-architecture/PORT_REVIEW.md)
applied the rule *"a port exists when there are two implementations or when a test double is
genuinely required"* and kept **three**:

* :class:`HealthProbePort` - two implementations exist now (a real adapter and a deterministic
  fake), and readiness correctness cannot be tested without controlling probe outcomes.
* :class:`JobRepositoryPort` - two implementations exist now (PostgreSQL and an in-memory
  double), and the retry/lease logic is the highest-risk code in Phase 1.
* :class:`IdempotencyStorePort` - same reasoning; the replay decision table has five branches
  that must all be exercised deterministically.

The other eight (``LLMProvider``, ``EmbeddingProvider``, ``Reranker``, ``VectorStore``,
``LexicalSearch``, ``DocumentParser``, ``ObjectStore``, ``AuthProvider``) are **deferred**, not
deleted: each returns in the phase that introduces its first alternative implementation.
Declaring a port for a capability with one implementation and no test need is an abstraction
that costs a file and an indirection and buys nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from knowledge_assistant.domain.health import ProbeCriticality, ProbeResult
from knowledge_assistant.domain.idempotency import IdempotencyRecord, IdempotencyScope
from knowledge_assistant.domain.jobs import Job, JobClaim

__all__ = [
    "HealthProbePort",
    "JobRepositoryPort",
    "IdempotencyStorePort",
    "JobContext",
    "JobHandlerFn",
    "JobHandlerRegistry",
    "handler_contract",
]


@runtime_checkable
class HealthProbePort(Protocol):
    """A named dependency check."""

    @property
    def name(self) -> str:
        """Return the stable, low-cardinality probe name used as a metric label."""
        ...

    @property
    def criticality(self) -> ProbeCriticality:
        """Return whether this probe gates readiness."""
        ...

    async def check(self) -> ProbeResult:
        """Run the check and return its result.

        Implementations must not raise: a probe that raises is an unhealthy probe, and the
        caller's timeout/exception handling converts it into ``ProbeStatus.FAILED`` with the
        exception attached as a dependency error code.
        """
        ...


@runtime_checkable
class JobRepositoryPort(Protocol):
    """Persistence for jobs, with atomic claim semantics.

    ``claim_next`` must be atomic across processes. In the PostgreSQL implementation that is
    ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a single transaction; the contract, not the
    mechanism, is what the worker depends on.
    """

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any],
        available_at: Any,
        max_attempts: int,
        idempotency_key: str | None = None,
    ) -> Job:
        """Insert a job, or return the existing job for a duplicate idempotency key.

        Args:
            job_type: Registered job type name.
            payload: JSON-serialisable job input.
            available_at: Earliest claim time (timezone-aware).
            max_attempts: Attempt ceiling, already clamped to the domain ceiling.
            idempotency_key: Optional client key making the insert idempotent.

        Returns:
            The created job, or the previously created job on a duplicate submission.

        """
        ...

    async def claim_next(self, *, now: Any, lease_seconds: float) -> JobClaim | None:
        """Atomically claim the next runnable job, or return ``None``.

        Args:
            now: Current time from the injected clock.
            lease_seconds: Lease duration to grant the claim.

        Returns:
            A ``JobClaim`` granting exclusive ownership for the lease window, or ``None`` if
            nothing is runnable.

        """
        ...

    async def mark_succeeded(self, *, claim: JobClaim, result: Mapping[str, Any]) -> None:
        """Record terminal success and store the result payload.

        Args:
            claim: The claim that executed the job.
            result: Handler result. Must be bounded in size; the caller truncates.

        """
        ...

    async def mark_retry(
        self,
        *,
        claim: JobClaim,
        error: str,
        next_available_at: Any,
    ) -> None:
        """Reschedule a job after a retryable failure.

        Args:
            claim: The claim that executed the job.
            error: Truncated failure summary.
            next_available_at: Earliest next claim time from the domain backoff policy.

        """
        ...

    async def mark_dead_lettered(self, *, claim: JobClaim, error: str) -> None:
        """Record terminal failure.

        Args:
            claim: The claim that executed the job.
            error: Truncated failure summary.

        """
        ...

    async def get(self, job_id: str) -> Job | None:
        """Return a job by identifier, or ``None``."""
        ...

    async def counts_by_state(self) -> Mapping[str, int]:
        """Return job counts keyed by state name, for the worker health probe."""
        ...

    async def dead_letter_count(self) -> int:
        """Return the number of dead-lettered jobs."""
        ...

    async def oldest_pending_age_seconds(self, *, now: Any) -> float | None:
        """Return the age of the oldest pending job, or ``None`` if the queue is empty.

        Used as the backlog signal in Phase 0's ``SLI-07``. Implemented as a single indexed
        aggregate rather than a full scan.
        """
        ...


@runtime_checkable
class IdempotencyStorePort(Protocol):
    """Persistence for idempotency records."""

    async def get(
        self, *, tenant_id: str, scope: IdempotencyScope, key: str
    ) -> IdempotencyRecord | None:
        """Return the record for a key, or ``None``."""
        ...

    async def begin(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        request_fingerprint: str,
        now: Any,
        retention_seconds: int,
    ) -> IdempotencyRecord | None:
        """Atomically claim the key for execution.

        Returns:
            ``None`` if the claim was obtained (caller should execute), or the existing record
            if another execution holds the key. This atomicity is the entire mechanism: two
            concurrent identical requests must not both execute.

        """
        ...

    async def complete(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        response_payload: Mapping[str, Any],
        now: Any,
    ) -> None:
        """Record a committed outcome for a claimed key."""
        ...

    async def release(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Release a claimed key after a failure, permitting a legitimate retry."""
        ...


class JobContext:
    """Execution context handed to a job handler.

    Carries only what a handler legitimately needs: the job identity and an attempt number.
    Deliberately does **not** carry a database connection, an HTTP client or a config object -
    a handler that needs one of those is a handler that has been given too much authority.
    """

    __slots__ = ("job_id", "job_type", "attempt", "tenant_id")

    def __init__(
        self,
        *,
        job_id: str,
        job_type: str,
        attempt: int,
        tenant_id: str = "local",
    ) -> None:
        """Build a handler context.

        Args:
            job_id: Identifier of the executing job.
            job_type: Registered type name.
            attempt: 1-based attempt number.
            tenant_id: Owning tenant. Phase 1 is single-tenant; the parameter exists so the
                worker never has to learn tenancy later.

        """
        self.job_id = job_id
        self.job_type = job_type
        self.attempt = attempt
        self.tenant_id = tenant_id

    def __repr__(self) -> str:
        """Return a safe debugging representation."""
        return f"JobContext(job_id={self.job_id!r}, attempt={self.attempt})"


#: A handler receives the payload and context, and returns a JSON-serialisable result.
#: It signals failure by raising; classification is then :func:`domain.jobs.classify_failure`.
#: Declared after ``JobContext`` because it references it.
type JobHandlerFn = Callable[[Mapping[str, Any], JobContext], Awaitable[Mapping[str, Any]]]

#: Registry shape passed to the job executor.
type JobHandlerRegistry = Mapping[str, JobHandlerFn]


def handler_contract() -> Sequence[str]:
    """Return the documented handler contract as strings, for documentation generation.

    Returns:
        The contract, one line per rule.

    """
    return (
        "async def handler(payload: Mapping[str, Any], ctx: JobContext) -> Mapping[str, Any]",
        "raise an ApplicationError subclass to fail; classification is automatic",
        "return a JSON-serialisable mapping to succeed",
        "handlers receive no clock, no database and no configuration",
    )

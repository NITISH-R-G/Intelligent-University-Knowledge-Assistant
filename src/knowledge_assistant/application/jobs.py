"""Job submission and execution use cases.

Two use cases, deliberately separated:

* :class:`SubmitJob` - enqueue, idempotently. Safe to call from a request handler.
* :class:`ExecuteJobOnce` - claim one job, run it, settle the outcome. Never called from a
  request handler.

The separation matters because the failure modes are opposite. Submission can fail with a
conflict or a dependency error and the client should know. Execution failure must **not** kill
the process; it is settled onto the job and the loop continues, because a single poisoned job
must never stop the pipeline (Phase 0 requirement FR-011).

Settlement is a single conditional UPDATE on ``(id, state, lease_expires_at)``. If the lease has
lapsed and another worker has taken the job, the UPDATE matches zero rows and this worker's
outcome is discarded. That is the fencing token: a slow worker that comes back to life cannot
overwrite a result recorded by the worker that took over.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Mapping
from typing import Any

from knowledge_assistant.application.ports import JobContext, JobHandlerFn, JobRepositoryPort
from knowledge_assistant.domain.clock import Clock
from knowledge_assistant.domain.errors import ConflictError, ValidationError
from knowledge_assistant.domain.jobs import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS_CEILING,
    FailureClass,
    Job,
    JobClaim,
    classify_failure,
    next_attempt_at,
    should_retry,
)

__all__ = ["SubmitJob", "ExecuteJobOnce", "JobExecutorOutcome"]

#: Maximum characters of a failure summary stored on the job row. Full detail goes to logs.
#: Bounding this keeps a large driver traceback out of a table that operators read.
MAX_STORED_ERROR_CHARS = 500


def _summarise_error(exc: BaseException) -> str:
    """Return a bounded, single-line failure summary safe to persist on a job row.

    Args:
        exc: Raised exception.

    Returns:
        Truncated ``type: message`` string, newline-collapsed and length-bounded.

    """
    message = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(text) > MAX_STORED_ERROR_CHARS:
        text = text[: MAX_STORED_ERROR_CHARS - 3] + "..."
    return text


class SubmitJob:
    """Enqueue a job, idempotently when a key is supplied."""

    __slots__ = ("_jobs", "_clock", "_max_attempts")

    def __init__(
        self,
        jobs: JobRepositoryPort,
        *,
        clock: Clock,
        max_attempts: int | None = None,
    ) -> None:
        """Build the use case.

        Args:
            jobs: Job repository.
            clock: Injected time source.
            max_attempts: Default attempt ceiling, clamped to ``MAX_ATTEMPTS_CEILING``.

        """
        self._jobs = jobs
        self._clock = clock
        self._max_attempts = min(max_attempts or DEFAULT_MAX_ATTEMPTS, MAX_ATTEMPTS_CEILING)

    async def execute(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any] | None = None,
        delay_seconds: float = 0.0,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
    ) -> Job:
        """Enqueue one job.

        Args:
            job_type: Registered job type.
            payload: JSON-serialisable input.
            delay_seconds: Delay before the job becomes claimable. Must be non-negative.
            idempotency_key: Optional client key. When supplied, a repeat submission with the
                same key returns the original job rather than creating a second one.
            max_attempts: Per-job override, clamped to the domain ceiling.

        Returns:
            The created job, or the existing job on duplicate submission.

        Raises:
            ValidationError: If ``job_type`` is blank or ``delay_seconds`` is negative.

        """
        if not job_type or not job_type.strip():
            msg = "job_type must be a non-empty string"
            raise ValidationError(msg, detail={"field": "job_type"})
        if delay_seconds < 0:
            msg = "delay_seconds must be >= 0"
            raise ValidationError(msg, detail={"field": "delay_seconds"})
        attempts = min(max_attempts or self._max_attempts, MAX_ATTEMPTS_CEILING)
        if attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValidationError(msg, detail={"field": "max_attempts"})
        now = self._clock.now()
        available_at = now + dt.timedelta(seconds=delay_seconds)
        return await self._jobs.enqueue(
            job_type=job_type,
            payload=dict(payload or {}),
            available_at=available_at,
            max_attempts=attempts,
            idempotency_key=idempotency_key,
        )


class JobExecutorOutcome:
    """Result of attempting one job. Returned for logging and metrics, never to a client."""

    __slots__ = (
        "job_id",
        "outcome",
        "state",
        "attempts_used",
        "failure_class",
        "next_available_at",
    )

    def __init__(
        self,
        *,
        job_id: str,
        outcome: str,
        state: str,
        attempts_used: int,
        failure_class: FailureClass | None = None,
        next_available_at: dt.datetime | None = None,
    ) -> None:
        """Build an outcome record."""
        self.job_id = job_id
        self.outcome = outcome
        self.state = state
        self.attempts_used = attempts_used
        self.failure_class = failure_class
        self.next_available_at = next_available_at

    def __repr__(self) -> str:
        """Return a safe debugging representation."""
        return f"JobExecutorOutcome(job_id={self.job_id!r}, outcome={self.outcome!r})"


class ExecuteJobOnce:
    """Claim one job, execute it, and settle the outcome."""

    __slots__ = (
        "_jobs",
        "_clock",
        "_handlers",
        "_lease_seconds",
        "_backoff_base",
        "_backoff_cap",
        "_jitter_ratio",
    )

    def __init__(
        self,
        jobs: JobRepositoryPort,
        handlers: Mapping[str, JobHandlerFn],
        *,
        clock: Clock,
        lease_seconds: float = 30.0,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 300.0,
        jitter_ratio: float = 0.2,
    ) -> None:
        """Build the use case.

        Args:
            jobs: Job repository.
            handlers: Registry of job type to handler. Unknown types are a permanent failure,
                not a crash.
            clock: Injected time source.
            lease_seconds: Lease granted on claim.
            backoff_base_seconds: First retry delay.
            backoff_cap_seconds: Retry delay ceiling.
            jitter_ratio: Jitter fraction applied to retry delays.

        """
        self._jobs = jobs
        self._handlers = dict(handlers)
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._jitter_ratio = jitter_ratio

    def can_handle(self, job_type: str) -> bool:
        """Return whether a handler is registered for ``job_type``."""
        return job_type in self._handlers

    async def execute_once(self) -> JobExecutorOutcome | None:
        """Claim and run at most one job.

        Returns:
            ``None`` when no job was claimable, otherwise the outcome. Never raises for a job
            failure: failures are settled onto the job. Raises only if the repository itself
            fails, because then we hold no claim and cannot settle anything.

        """
        now = self._clock.now()
        claim = await self._jobs.claim_next(now=now, lease_seconds=self._lease_seconds)
        if claim is None:
            return None
        return await self._execute_claim(claim)

    async def _execute_claim(self, claim: JobClaim) -> JobExecutorOutcome:
        """Run one claimed job and persist its outcome.

        Args:
            claim: Ownership proof from the repository.

        Returns:
            The outcome record.

        """
        job = claim.job
        handler = self._handlers.get(job.type)
        if handler is None:
            # Unknown job type is permanent: retrying cannot make a missing handler appear, and
            # retrying would hide a deployment defect behind a queue of doomed jobs.
            summary = f"no handler registered for job type {job.type!r}"
            await self._jobs.mark_dead_lettered(claim=claim, error=summary)
            return JobExecutorOutcome(
                job_id=job.id,
                outcome="dead_lettered",
                state=job.state.value,
                attempts_used=job.attempts_used,
                failure_class=FailureClass.PERMANENT,
            )

        ctx = JobContext(job_id=job.id, job_type=job.type, attempt=job.attempts_used)
        try:
            result = await handler(dict(job.payload), ctx)
        except BaseException as exc:  # noqa: BLE001 - settlement must cover cancellation too
            return await self._settle_failure(claim, exc)
        await self._jobs.mark_succeeded(claim=claim, result=result)
        return JobExecutorOutcome(
            job_id=job.id,
            outcome="succeeded",
            state=job.state.value,
            attempts_used=job.attempts_used,
        )

    async def _settle_failure(self, claim: JobClaim, exc: BaseException) -> JobExecutorOutcome:
        """Decide between retry and dead-letter, then persist that decision.

        Cancellation deserves a note: a worker shutting down mid-job must not dead-letter the
        job, because the work was not actually attempted. ``asyncio.CancelledError`` is treated
        as transient and retried, and the lease is allowed to lapse if settlement itself is
        cancelled.

        Args:
            claim: Ownership proof.
            exc: Exception raised by the handler or by settlement.

        Returns:
            The outcome record.

        """
        job = claim.job
        failure_class = classify_failure(exc)
        attempts_used = job.attempts_used + 1
        summary = _summarise_error(exc)
        retry = should_retry(
            failure_class=failure_class,
            attempts_used=attempts_used,
            max_attempts=job.max_attempts,
        )
        if not retry:
            await self._jobs.mark_dead_lettered(claim=claim, error=summary)
            return JobExecutorOutcome(
                job_id=job.id,
                outcome="dead_lettered",
                state=job.state.value,
                attempts_used=attempts_used,
                failure_class=failure_class,
            )
        now = self._clock.now()
        when = next_attempt_at(
            now=now,
            attempt=attempts_used,
            base_seconds=self._backoff_base,
            cap_seconds=self._backoff_cap,
            unit_interval=secrets.randbelow(1_000_000) / 1_000_000.0,
            jitter_ratio=self._jitter_ratio,
        )
        await self._jobs.mark_retry(claim=claim, error=summary, next_available_at=when)
        return JobExecutorOutcome(
            job_id=job.id,
            outcome="retry_scheduled",
            state=job.state.value,
            attempts_used=attempts_used,
            failure_class=failure_class,
            next_available_at=when,
        )


def make_conflict(message: str) -> ConflictError:
    """Return a ConflictError with a fixed message. Small helper to keep imports local."""
    return ConflictError(message, detail={"resource_type": "job"})

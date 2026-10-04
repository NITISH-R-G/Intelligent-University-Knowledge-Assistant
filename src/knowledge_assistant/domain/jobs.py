"""Job domain model: lifecycle, retry classification and backoff.

Everything here is a **pure function or an immutable value**. No database, no clock, no
logging. That is what makes the retry policy property-testable, which matters because retry
policy is where self-inflicted outages live.

The two decisions this module encodes, and their rationale:

**Retry classification is explicit, not inferred.** A handler must state whether a failure is
transient, permanent or unknown. Inferring it from exception type invites the classic bug where
a malformed input is retried forever because it raises something retry-shaped.

**Jitter is mandatory.** A retry storm after an outage recovery synchronises every client to the
same instant and reproduces the outage. Delay therefore includes randomness, and the randomness
is an injected value so tests can pin it.
"""

from __future__ import annotations

import datetime as dt
import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

__all__ = [
    "JobState",
    "FailureClass",
    "Job",
    "JobClaim",
    "MAX_ATTEMPTS_CEILING",
    "DEFAULT_MAX_ATTEMPTS",
    "is_terminal",
    "base_delay_seconds",
    "apply_jitter",
    "next_attempt_at",
    "should_retry",
    "classify_failure",
]


class JobState(enum.StrEnum):
    """Lifecycle states. Transitions are one-way except ``RETRY`` which returns to PENDING.

    State diagram (enforced by :func:`assert_transition_allowed`):

        PENDING ──claim──▶ RUNNING ──success──▶ SUCCEEDED
           ▲                  │
           │                  ├──failure, retries left ──▶ RETRY ──▶ PENDING
           │                  │
           └──────lease expiry┘
                              ├──failure, no retries ──▶ DEAD_LETTERED
                              └──fenced/stale ──▶ (released by lease expiry)
    """

    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"


#: States from which no further work happens without operator or scheduler action.
_TERMINAL: Final[frozenset[JobState]] = frozenset({JobState.SUCCEEDED, JobState.DEAD_LETTERED})

#: Hard ceiling on attempts. Exists so that a misconfigured ``max_attempts`` in the
#: environment cannot create an unbounded retry loop. This is a safety rail, not a policy:
#: the real limit is per job type and is lower.
MAX_ATTEMPTS_CEILING: Final[int] = 10

#: Default attempt ceiling when a job type does not specify one.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: Largest exponent for which ``2.0 ** exponent`` is still a finite float. Beyond it the
#: intermediate overflows even though the capped result is perfectly well defined.
_MAX_FINITE_POWER_OF_TWO_EXPONENT: Final[int] = 1023

_ALLOWED_TRANSITIONS: Final[Mapping[JobState, frozenset[JobState]]] = {
    JobState.PENDING: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset(
        {JobState.SUCCEEDED, JobState.RETRY, JobState.DEAD_LETTERED, JobState.PENDING}
    ),
    JobState.RETRY: frozenset({JobState.PENDING}),
    JobState.SUCCEEDED: frozenset(),
    JobState.DEAD_LETTERED: frozenset(),
}


class FailureClass(enum.StrEnum):
    """How a handler classified a failure.

    ``UNKNOWN`` is deliberately a first-class value. It retries a bounded number of times and
    then dead-letters, which is the safe default: assuming "permanent" would silently drop work
    after one transient blip, and assuming "transient" would spin forever.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


def is_terminal(state: JobState) -> bool:
    """Return whether ``state`` permits no further automatic progress.

    Args:
        state: Current job state.

    Returns:
        ``True`` for SUCCEEDED and DEAD_LETTERED.

    """
    return state in _TERMINAL


def assert_transition_allowed(current: JobState, target: JobState) -> None:
    """Raise if a state transition is not permitted.

    Args:
        current: State the job is in.
        target: State being moved to.

    Raises:
        ValueError: If the transition is not in the allowed set. This is a programming
            error, not a runtime condition, hence ``ValueError`` rather than an
            ``ApplicationError``: it must never be reported to a client.

    """
    if target not in _ALLOWED_TRANSITIONS[current]:
        msg = f"illegal job state transition {current.value} -> {target.value}"
        raise ValueError(msg)


def classify_failure(exc: BaseException) -> FailureClass:
    """Classify an exception as transient, permanent or unknown.

    The mapping is deliberately small and explicit. Anything not named is ``UNKNOWN``,
    which retries a bounded number of times before dead-lettering.

    Args:
        exc: Exception raised by a handler.

    Returns:
        The failure class.

    """
    # Imported lazily inside the function to keep this module import-cycle free if the
    # error taxonomy is ever moved.
    from knowledge_assistant.domain.errors import (  # noqa: PLC0415
        ConflictError,
        DependencyUnavailableError,
        RateLimitedError,
        TimeoutError_,
        ValidationError,
    )

    if isinstance(exc, (DependencyUnavailableError, TimeoutError_, RateLimitedError)):
        return FailureClass.TRANSIENT
    if isinstance(exc, (ValidationError, ConflictError)):
        return FailureClass.PERMANENT
    return FailureClass.UNKNOWN


def base_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 300.0,
) -> float:
    """Return the un-jittered exponential backoff delay for an attempt number.

    Doubling with a cap. The cap exists because pure exponential backoff on a long outage
    pushes the retry past the point where a human notices the system is broken.

    Args:
        attempt: 1-based attempt number that just failed. Attempt 1 uses ``base_seconds``.
        base_seconds: Delay after the first failure.
        cap_seconds: Maximum delay regardless of attempt number.

    Returns:
        Delay in seconds, in ``(0, cap_seconds]``.

    Raises:
        ValueError: If ``attempt`` is less than 1, or a bound is non-positive.

    """
    if attempt < 1:
        msg = "attempt must be >= 1"
        raise ValueError(msg)
    if base_seconds <= 0 or cap_seconds <= 0:
        msg = "base_seconds and cap_seconds must be positive"
        raise ValueError(msg)
    if cap_seconds <= base_seconds:
        # The cap already swallows the first delay, so every attempt waits the cap.
        return cap_seconds
    # Past a certain attempt the cap, not the doubling series, decides the answer. Compare
    # exponents *before* multiplying: ``2.0 ** (attempt - 1)`` raises OverflowError for a large
    # attempt even though the result is irrelevant, and a retry path must never raise.
    # log2 is taken as a difference so that ``cap_seconds / base_seconds`` cannot overflow.
    cap_exponent = math.ceil(math.log2(cap_seconds) - math.log2(base_seconds))
    exponent = min(attempt - 1, cap_exponent, _MAX_FINITE_POWER_OF_TWO_EXPONENT)
    return min(base_seconds * (2.0**exponent), cap_seconds)


def apply_jitter(delay_seconds: float, unit_interval: float, *, ratio: float = 0.2) -> float:
    """Apply symmetric jitter to a delay.

    ``full`` (equal to the delay) is subtracted or added by ``ratio`` of the delay, so the
    result lies in ``[delay*(1-ratio), delay*(1+ratio)]``. Symmetric jitter around the mean
    keeps the expected retry time at ``delay``; one-sided jitter would bias every retry late.

    Args:
        delay_seconds: Un-jittered delay.
        unit_interval: Random value in ``[0, 1)``. Injected so tests can pin it.
        ratio: Maximum fractional deviation, in ``[0, 1]``.

    Returns:
        Jittered delay, never negative.

    Raises:
        ValueError: If ``unit_interval`` is outside ``[0, 1)`` or ``ratio`` outside ``[0, 1]``.

    """
    if not 0.0 <= unit_interval < 1.0:
        msg = "unit_interval must be in [0, 1)"
        raise ValueError(msg)
    if not 0.0 <= ratio <= 1.0:
        msg = "ratio must be in [0, 1]"
        raise ValueError(msg)
    if delay_seconds <= 0:
        msg = "delay_seconds must be positive"
        raise ValueError(msg)
    # Map [0,1) onto [-1,1): the midpoint of the range is the un-jittered delay.
    offset = (unit_interval * 2.0) - 1.0
    return max(0.0, delay_seconds * (1.0 + (offset * ratio)))


def next_attempt_at(
    *,
    now: dt.datetime,
    attempt: int,
    base_seconds: float,
    cap_seconds: float,
    unit_interval: float,
    jitter_ratio: float = 0.2,
) -> dt.datetime:
    """Return the earliest time a job may next be claimed.

    Args:
        now: Current time from the injected clock.
        attempt: 1-based number of the attempt that just failed.
        base_seconds: Backoff base.
        cap_seconds: Backoff cap.
        unit_interval: Jitter source in ``[0, 1)``.
        jitter_ratio: Jitter fraction.

    Returns:
        Timezone-aware UTC timestamp.

    """
    delay = apply_jitter(
        base_delay_seconds(attempt, base_seconds=base_seconds, cap_seconds=cap_seconds),
        unit_interval,
        ratio=jitter_ratio,
    )
    return now + dt.timedelta(seconds=delay)


def should_retry(
    *,
    failure_class: FailureClass,
    attempts_used: int,
    max_attempts: int,
) -> bool:
    """Decide whether a failed attempt may be retried.

    Args:
        failure_class: Classification from :func:`classify_failure`.
        attempts_used: How many attempts have already been consumed, including this one.
        max_attempts: Ceiling for this job type, already clamped by the caller to
            :data:`MAX_ATTEMPTS_CEILING`.

    Returns:
        ``True`` if the job should be rescheduled; ``False`` if it must dead-letter now.

    Notes:
        A ``PERMANENT`` failure never retries. An ``UNKNOWN`` failure retries up to the
        ceiling, because treating unknown as permanent would lose work to a single blip.

    """
    if failure_class is FailureClass.PERMANENT:
        return False
    return attempts_used < max_attempts


@dataclass(frozen=True, slots=True)
class Job:
    """An immutable snapshot of a unit of asynchronous work.

    Immutability is load-bearing: a job row can be handed to a handler without the handler
    being able to mutate state another process is also reading. Persistence replaces the
    record rather than mutating it.
    """

    id: str
    type: str
    state: JobState
    attempts_used: int
    max_attempts: int
    available_at: dt.datetime
    lease_expires_at: dt.datetime | None
    payload: Mapping[str, Any]
    last_error: str | None = None
    created_at: dt.datetime | None = None

    @property
    def is_terminal(self) -> bool:
        """Return whether the job has reached a terminal state."""
        return is_terminal(self.state)

    def with_transition(
        self,
        target: JobState,
        *,
        now: dt.datetime,
        error: str | None = None,
        next_available_at: dt.datetime | None = None,
    ) -> Job:
        """Return a new job in ``target`` state, validating the transition.

        Args:
            target: State to move to.
            now: Current time; used to set ``available_at`` when retrying.
            error: Failure summary, truncated by the caller before being stored. Full
                detail belongs in logs, not in a row that support staff may read.
            next_available_at: Explicit next claim time, overriding the computed backoff.

        Returns:
            A new ``Job``; the original is unchanged.

        Raises:
            ValueError: If the transition is illegal.

        """
        assert_transition_allowed(self.state, target)
        attempts = (
            self.attempts_used + 1
            if target in (JobState.RUNNING, JobState.RETRY)
            else self.attempts_used
        )
        return replace(
            self,
            state=target,
            attempts_used=attempts,
            lease_expires_at=None if target is not JobState.RUNNING else self.lease_expires_at,
            available_at=next_available_at or self.available_at,
            last_error=error or self.last_error,
            created_at=self.created_at or now,
        )


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Proof that a worker owns a job for a bounded period.

    The lease is the entire ownership mechanism. There is no heartbeat and no lock table: a
    worker that dies stops renewing, and the lease lapses. This means a stuck worker cannot
    block the queue forever, at the cost of at-least-once delivery, which the idempotency
    contract absorbs.
    """

    job: Job
    claimed_at: dt.datetime
    lease_expires_at: dt.datetime

    def is_expired(self, *, now: dt.datetime) -> bool:
        """Return whether the lease has lapsed at ``now``.

        Args:
            now: Current time from the injected clock.

        Returns:
            ``True`` if the claim may be taken over by another worker.

        """
        return now >= self.lease_expires_at

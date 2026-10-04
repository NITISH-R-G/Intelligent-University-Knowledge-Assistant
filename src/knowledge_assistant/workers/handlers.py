"""Job handlers registered in Phase 1.

**There is no ingestion here.** Phase 1's job system exists to prove the queue, lease, retry,
dead-letter and observability mechanics work end to end, with one trivial handler that does
something observable and deterministic.

Two handlers are registered deliberately beyond the happy path:

* :func:`failing_handler` - always raises. Registered so that failure injection is a supported,
  tested behaviour rather than an untested code path.
* :func:`flaky_handler` - fails a configured number of times, then succeeds. Registered to
  exercise the retry path *and* the backoff schedule without waiting on real backoff.

Handlers are the only place in the system permitted to raise for a business reason. They signal
failure by raising; :func:`knowledge_assistant.domain.jobs.classify_failure` decides whether
that is retryable, and the worker settles it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from knowledge_assistant.application.ports import JobHandlerFn
from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    InternalError,
    ValidationError,
)
from knowledge_assistant.domain.jobs import DEFAULT_MAX_ATTEMPTS

__all__ = [
    "ECHO_JOB_TYPE",
    "FAILING_JOB_TYPE",
    "FLAKY_JOB_TYPE",
    "echo_handler",
    "failing_handler",
    "flaky_handler",
    "build_registry",
]

#: Echoes its payload back. The Phase 1 proof that the pipeline works.
ECHO_JOB_TYPE = "system.echo"

#: Always fails. Used by failure-injection tests.
FAILING_JOB_TYPE = "system.failing"

#: Fails N times then succeeds. Exercises retry and backoff.
FLAKY_JOB_TYPE = "system.flaky"


async def echo_handler(payload: Mapping[str, Any], ctx: Any) -> Mapping[str, Any]:
    """Echo the payload with execution metadata.

    Args:
        payload: Job input.
        ctx: Job context.

    Returns:
        The payload plus ``job_id``, ``attempt`` and ``handled_at`` (from the injected clock,
        via the context's absence: the handler has no clock, so it reports no timestamp - a
        deliberate demonstration that handlers get no ambient time).

    """
    return {
        "echoed": dict(payload),
        "job_id": ctx.job_id,
        "attempt": ctx.attempt,
    }


async def failing_handler(payload: Mapping[str, Any], ctx: Any) -> Mapping[str, Any]:
    """Raise immediately, with a failure class chosen by the payload.

    Args:
        payload: May contain ``failure_kind``: ``transient``, ``permanent`` or ``unknown``.
            Default is ``unknown``, which retries to the ceiling and then dead-letters.
        ctx: Job context (unused).

    Returns:
        Never returns.

    Raises:
        DependencyUnavailableError: For ``transient``.
        ValidationError: For ``permanent``.
        InternalError: For ``unknown`` or any unrecognised value.

    """
    del ctx
    kind = str(payload.get("failure_kind", "unknown"))
    if kind == "transient":
        raise DependencyUnavailableError(
            "simulated transient dependency failure", dependency="simulated"
        )
    if kind == "permanent":
        raise ValidationError(
            "simulated permanent validation failure", detail={"field": "simulated"}
        )
    raise InternalError("simulated unknown failure")


async def flaky_handler(payload: Mapping[str, Any], ctx: Any) -> Mapping[str, Any]:
    """Fail until the configured attempt number, then succeed.

    Args:
        payload: May contain ``fail_until_attempt`` (default 1), meaning attempts strictly less
            than that value fail.
        ctx: Job context.

    Returns:
        A success payload once the threshold is reached.

    Raises:
        DependencyUnavailableError: On the attempts before the threshold.

    """
    threshold = int(payload.get("fail_until_attempt", 1))
    if ctx.attempt < threshold:
        raise DependencyUnavailableError(
            f"simulated transient failure on attempt {ctx.attempt} of {threshold}",
            dependency="simulated",
        )
    return {"succeeded_on_attempt": ctx.attempt, "threshold": threshold}


def build_registry() -> dict[str, JobHandlerFn]:
    """Return the Phase 1 handler registry.

    Returns:
        Mapping of job type to handler. Unknown types are a permanent failure at execution time.

    """
    return {
        ECHO_JOB_TYPE: echo_handler,
        FAILING_JOB_TYPE: failing_handler,
        FLAKY_JOB_TYPE: flaky_handler,
    }


def default_max_attempts() -> int:
    """Return the default attempt ceiling for Phase 1 job types."""
    return DEFAULT_MAX_ATTEMPTS

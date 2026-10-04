"""Benchmark execution harness.

Measures one callable, repeatedly, and reports a distribution. Deliberately small: the
hard parts of benchmarking are not the loop, they are declaring honestly what was
measured and keeping runs comparable. This module does the loop and refuses to report
anything it did not actually observe.

Three decisions worth stating.

**``perf_counter_ns`` for timing.** It is monotonic, so a clock adjustment mid-run cannot
produce a negative duration, and it is the highest-resolution clock available without a
dependency. The default ``time.time`` has neither property.

**Warm-up iterations are executed and discarded, never reported.** First-call costs are
real and they are not the steady state: module import, lazy initialisation, CPU cache
misses and allocator warm-up. Mixing them into the distribution is what makes a
micro-benchmark report a p99 that no user will ever experience. The count is still
recorded on the result, so a reader knows how much was discarded and why.

**A benchmark that cannot run reports BLOCKED with a reason.** It never reports zero.
Zero milliseconds is a claim - an excellent one - and a blocked benchmark has made no
claim at all. The distinction is the single most important thing in this file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from benchmarks.framework.environment import capture_environment
from benchmarks.framework.results import (
    BenchmarkResult,
    MeasurementScope,
    Status,
    compute_statistics,
)

__all__ = [
    "Benchmark",
    "blocked",
    "run",
    "run_async",
    "DEFAULT_ITERATIONS",
    "DEFAULT_WARMUP_ITERATIONS",
]

#: Measured iterations. Chosen as a methodology default, not a performance requirement:
#: enough samples for a stable p99, few enough that the full suite stays fast enough to
#: run on every change. See the protocol's "Number of iterations".
DEFAULT_ITERATIONS: int = 200

#: Discarded iterations before measurement begins. See the module docstring.
DEFAULT_WARMUP_ITERATIONS: int = 20

T = TypeVar("T")


class Benchmark:
    """A unit of work to measure.

    Attributes:
        name: Stable identifier, also the key used for baseline comparison.
        units: Unit of the measured value, e.g. ``"milliseconds"`` or ``"bytes"``.
        scope: What the measurement covers. See :class:`MeasurementScope`.
        notes: Free text carried into the result, typically stating what the number does
            NOT include.

    """

    __slots__ = ("name", "units", "scope", "notes")

    def __init__(
        self,
        name: str,
        *,
        units: str,
        scope: MeasurementScope = MeasurementScope.IN_PROCESS,
        notes: str = "",
    ) -> None:
        """Describe a benchmark.

        Args:
            name: Stable identifier.
            units: Unit of the measured value.
            scope: What the measurement covers.
            notes: Caveat text attached to the result.

        """
        self.name = name
        self.units = units
        self.scope = scope
        self.notes = notes


def _now() -> str:
    """Return an ISO-8601 UTC timestamp.

    Returns:
        Timestamp with a trailing ``Z``, e.g. ``2026-10-04T12:00:00.000000Z``.

    """
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def blocked(benchmark: Benchmark, reason: str) -> BenchmarkResult:
    """Build a BLOCKED result for a benchmark that could not run.

    Args:
        benchmark: The benchmark that did not run.
        reason: Why. Must be specific enough to tell the reader whether retrying elsewhere
            would help - "no Docker daemon" is actionable, "error" is not.

    Returns:
        A result carrying no statistics.

    """
    return BenchmarkResult(
        name=benchmark.name,
        status=Status.BLOCKED,
        scope=MeasurementScope.BLOCKED,
        units=benchmark.units,
        iterations=0,
        warmup_iterations=0,
        timestamp=_now(),
        environment=capture_environment(),
        blocked_reason=reason,
        notes=benchmark.notes,
    )


def _measure_loop(operation: Callable[[], Any], iterations: int) -> list[float]:
    """Time ``operation`` once per call, returning milliseconds.

    Args:
        operation: Zero-argument callable.
        iterations: Number of measured calls.

    Returns:
        One duration per call, in milliseconds.

    """
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        elapsed_ns = time.perf_counter_ns() - started
        samples.append(elapsed_ns / 1_000_000.0)
    return samples


def run(
    benchmark: Benchmark,
    operation: Callable[[], Any],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
) -> BenchmarkResult:
    """Measure a synchronous operation.

    Args:
        benchmark: Description of the work.
        operation: Zero-argument callable to time. Its return value is discarded.
        iterations: Measured iterations.
        warmup_iterations: Discarded iterations run first.

    Returns:
        The result, or an ERROR result if the operation raised.

    """
    if iterations < 1:
        return BenchmarkResult(
            name=benchmark.name,
            status=Status.ERROR,
            scope=benchmark.scope,
            units=benchmark.units,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            timestamp=_now(),
            environment=capture_environment(),
            error=f"iterations must be >= 1, got {iterations}",
            notes=benchmark.notes,
        )
    try:
        for _ in range(warmup_iterations):
            operation()
        samples = _measure_loop(operation, iterations)
    except Exception as exc:  # noqa: BLE001 - a failing benchmark is a result, not a crash
        return BenchmarkResult(
            name=benchmark.name,
            status=Status.ERROR,
            scope=benchmark.scope,
            units=benchmark.units,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            timestamp=_now(),
            environment=capture_environment(),
            error=f"{type(exc).__name__}: {exc}",
            notes=benchmark.notes,
        )

    return BenchmarkResult(
        name=benchmark.name,
        status=Status.OK,
        scope=benchmark.scope,
        units=benchmark.units,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        timestamp=_now(),
        environment=capture_environment(),
        statistics=compute_statistics(samples),
        notes=benchmark.notes,
    )


def run_async(
    benchmark: Benchmark,
    operation: Callable[[], Awaitable[Any]],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
) -> BenchmarkResult:
    """Measure an asynchronous operation.

    Synchronous by design, so it is a drop-in sibling of :func:`run` and callers cannot
    accidentally forget to await it. One event loop is created for the *whole* benchmark
    and every iteration is awaited inside it. Constructing a loop per iteration would
    measure loop construction and teardown rather than the operation, which for a fast
    in-process coroutine is the larger cost by a wide margin.

    Args:
        benchmark: Description of the work.
        operation: Zero-argument callable returning an awaitable.
        iterations: Measured iterations.
        warmup_iterations: Discarded iterations run first.

    Returns:
        The result, or an ERROR result if the operation raised.

    """
    if iterations < 1:
        return BenchmarkResult(
            name=benchmark.name,
            status=Status.ERROR,
            scope=benchmark.scope,
            units=benchmark.units,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            timestamp=_now(),
            environment=capture_environment(),
            error=f"iterations must be >= 1, got {iterations}",
            notes=benchmark.notes,
        )

    async def _measure() -> list[float]:
        """Run warm-up then the measured loop inside the live event loop."""
        for _ in range(warmup_iterations):
            await operation()
        samples: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter_ns()
            await operation()
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
        return samples

    try:
        samples = asyncio.run(_measure())
    except Exception as exc:  # noqa: BLE001 - a failing benchmark is a result, not a crash
        return BenchmarkResult(
            name=benchmark.name,
            status=Status.ERROR,
            scope=benchmark.scope,
            units=benchmark.units,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            timestamp=_now(),
            environment=capture_environment(),
            error=f"{type(exc).__name__}: {exc}",
            notes=benchmark.notes,
        )

    return BenchmarkResult(
        name=benchmark.name,
        status=Status.OK,
        scope=benchmark.scope,
        units=benchmark.units,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        timestamp=_now(),
        environment=capture_environment(),
        statistics=compute_statistics(samples),
        notes=benchmark.notes,
    )


def is_async(operation: Callable[[], Any]) -> bool:
    """Return whether a callable returns an awaitable when invoked with no arguments.

    Used so a suite can hand both sync and async work to one entry point without
    repeating the branch.

    Args:
        operation: The callable to inspect.

    Returns:
        ``True`` if the callable is declared ``async def``.

    """
    return inspect.iscoroutinefunction(operation)


def summarise(results: Sequence[BenchmarkResult]) -> str:
    """Render a human-readable summary table.

    Args:
        results: Results to summarise, in display order.

    Returns:
        A fixed-width table. Blocked rows show the reason instead of numbers, so a
        blocked benchmark can never be misread as a fast one.

    """
    if not results:
        return "no benchmarks executed"
    name_width = max(len(r.name) for r in results)
    lines = [
        f"{'benchmark'.ljust(name_width)}  {'status':<8}  {'scope':<14}  "
        f"{'p50':>10}  {'p95':>10}  {'p99':>10}  units"
    ]
    lines.append("-" * (name_width + 62))
    for result in results:
        if result.status is Status.OK and result.statistics is not None:
            stats = result.statistics
            lines.append(
                f"{result.name.ljust(name_width)}  {str(result.status):<8}  "
                f"{str(result.scope):<14}  {stats.p50:>10.4f}  {stats.p95:>10.4f}  "
                f"{stats.p99:>10.4f}  {result.units}"
            )
        elif result.status is Status.BLOCKED:
            lines.append(
                f"{result.name.ljust(name_width)}  {str(result.status):<8}  "
                f"{str(result.scope):<14}  {'-':>10}  {'-':>10}  {'-':>10}  "
                f"{result.blocked_reason}"
            )
        else:
            lines.append(
                f"{result.name.ljust(name_width)}  {str(result.status):<8}  "
                f"{str(result.scope):<14}  {'-':>10}  {'-':>10}  {'-':>10}  {result.error}"
            )
    return "\n".join(lines)

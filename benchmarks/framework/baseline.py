"""Baseline capture and regression detection.

A benchmark that reports numbers nobody compares is a benchmark nobody reads. This module
answers the only question that matters after a run: did anything get slower than it was?

**The threshold here is not a performance requirement.** Phase 0 defines fourteen PERF
requirements and every one of them describes search, retrieval, generation or ingestion -
functionality that does not exist in Phase 1. There is deliberately no Phase 0 target for
"how fast should configuration parsing be", so this module must not invent one. What it
does have is a *sensitivity*: how much slower than its own previous self a benchmark must
get before a human is asked to look. That is a statement about noise, not about adequacy.

The distinction matters because the two are used differently. A performance requirement
says "this must be fast enough for a user"; a sensitivity says "this changed enough to
warrant an explanation". Treating the second as the first is how a suite ends up
greenlighting a system that is technically no worse than it was and completely inadequate.

Comparisons are refused unless the scope and units match. Comparing a p95 in
milliseconds against a p95 in microseconds, or an in-process number against a live-database
one, produces a confident and meaningless verdict, so it is not offered.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Final

from benchmarks.framework.results import BenchmarkResult, MeasurementScope, Status

__all__ = [
    "Comparison",
    "DEFAULT_REGRESSION_TOLERANCE",
    "compare",
    "load_baseline",
    "save_baseline",
]

#: Relative p95 increase that counts as a regression.
#:
#: MEASUREMENT METHODOLOGY - NOT A PERFORMANCE REQUIREMENT. Phase 0 defines no Phase 1
#: performance target, so there is nothing to compare against; this is a noise threshold.
#:
#: Chosen from measurement, not from taste. Five consecutive warm runs on the development
#: host produced a coefficient of variation of 4.8 % for ``config.load_settings`` and
#: 10.9 % for ``api.request_healthz``, with a max-min spread of 30.6 % on the latter. A
#: 20 % threshold - the obvious round number - therefore produces false regressions on an
#: unchanged commit, which is the fastest way to teach a team to ignore the benchmark.
#: 30 % sits above the observed noise while remaining far below the 2-10x change a real
#: regression (a lost cache, a regex compiled per request) produces.
#:
#: Caveat worth carrying: a threshold that large will miss a genuine 25 % regression.
#: In-process micro-benchmarks on a shared machine cannot do better than this. When a
#: benchmark needs finer resolution it needs a quieter machine and more iterations, not a
#: smaller number here.
DEFAULT_REGRESSION_TOLERANCE: Final[float] = 0.30

#: A percentage is a fraction scaled by 100. Named so the conversion reads as arithmetic
#: rather than as a stray literal in a format string.
_PERCENT: Final[float] = 1.0


@dataclasses.dataclass(frozen=True, slots=True)
class Comparison:
    """Outcome of comparing one result against one baseline entry."""

    name: str
    verdict: str
    baseline_p95: float | None
    current_p95: float | None
    relative_change: float | None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the comparison as a JSON-serialisable mapping.

        Returns:
            Field name to value.

        """
        return dataclasses.asdict(self)


def _relative_change(baseline: float, current: float) -> float | None:
    """Return the fractional change from ``baseline`` to ``current``.

    Args:
        baseline: Previous p95.
        current: Current p95.

    Returns:
        Fractional change, or ``None`` when the baseline is zero - a percentage change
        from zero is undefined, and dividing by it would produce infinity rather than an
        honest "cannot compare".

    """
    if baseline <= 0.0:
        return None
    return (current - baseline) / baseline


def _guard(current: BenchmarkResult, baseline: BenchmarkResult | None) -> Comparison | None:
    """Return a comparison for a state that cannot be compared, or ``None`` to proceed.

    Split out from :func:`compare` so the comparability rules read as one list rather
    than being buried in a chain of early returns.

    Args:
        current: The result just measured.
        baseline: The previously recorded result, or ``None``.

    Returns:
        A ``skipped`` / ``new`` / ``incomparable`` comparison, or ``None`` when the two
        results may be compared directly.

    """
    current_p95 = current.statistics.p95 if current.statistics is not None else None
    if current.status is not Status.OK or current.statistics is None:
        return Comparison(
            name=current.name,
            verdict="skipped",
            baseline_p95=None,
            current_p95=None,
            relative_change=None,
            reason=f"current result is {current.status}, not measured",
        )
    if baseline is None:
        return Comparison(
            name=current.name,
            verdict="new",
            baseline_p95=None,
            current_p95=current_p95,
            relative_change=None,
            reason="no baseline recorded",
        )
    baseline_p95 = baseline.statistics.p95 if baseline.statistics is not None else None
    if baseline.status is not Status.OK or baseline.statistics is None:
        return Comparison(
            name=current.name,
            verdict="incomparable",
            baseline_p95=None,
            current_p95=current_p95,
            relative_change=None,
            reason=f"baseline is {baseline.status}, not measured",
        )
    if baseline.units != current.units:
        return Comparison(
            name=current.name,
            verdict="incomparable",
            baseline_p95=baseline_p95,
            current_p95=current_p95,
            relative_change=None,
            reason=f"units differ: baseline {baseline.units!r} vs current {current.units!r}",
        )
    if baseline.scope is not current.scope:
        return Comparison(
            name=current.name,
            verdict="incomparable",
            baseline_p95=baseline_p95,
            current_p95=current_p95,
            relative_change=None,
            reason=f"scope differs: baseline {baseline.scope} vs current {current.scope}",
        )
    return None


def compare(
    current: BenchmarkResult,
    baseline: BenchmarkResult | None,
    *,
    tolerance: float = DEFAULT_REGRESSION_TOLERANCE,
) -> Comparison:
    """Compare one result against a baseline entry.

    Args:
        current: The result just measured.
        baseline: The previously recorded result, or ``None`` when there is none.
        tolerance: Fractional p95 increase treated as a regression.

    Returns:
        A comparison whose verdict is one of ``regression``, ``improvement``,
        ``unchanged``, ``new``, ``skipped`` or ``incomparable``.

    """
    guard = _guard(current, baseline)
    if guard is not None:
        return guard

    assert current.statistics is not None  # noqa: S101 - guaranteed by _guard
    assert baseline is not None and baseline.statistics is not None  # noqa: S101
    change = _relative_change(baseline.statistics.p95, current.statistics.p95)
    if change is None:
        return Comparison(
            name=current.name,
            verdict="incomparable",
            baseline_p95=baseline.statistics.p95,
            current_p95=current.statistics.p95,
            relative_change=None,
            reason="baseline p95 is zero; a relative change is undefined",
        )
    if change > tolerance:
        verdict = "regression"
    elif change < -tolerance:
        verdict = "improvement"
    else:
        verdict = "unchanged"
    return Comparison(
        name=current.name,
        verdict=verdict,
        baseline_p95=baseline.statistics.p95,
        current_p95=current.statistics.p95,
        relative_change=change,
    )


def save_baseline(path: Path, results: list[BenchmarkResult]) -> None:
    """Write results to a baseline file.

    Args:
        path: Destination file.
        results: Results to record.

    Raises:
        ValueError: If any result does not validate.

    """
    for result in results:
        result.validate()
    document = {
        "schema_version": results[0].schema_version if results else "1.0",
        "results": [r.as_dict() for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def load_baseline(path: Path) -> dict[str, BenchmarkResult]:
    """Read a baseline file into results keyed by benchmark name.

    Args:
        path: Baseline file.

    Returns:
        Name to result. Empty when the file does not exist - a missing baseline means
        every benchmark is ``new``, which is a normal first-run state rather than an
        error.

    Raises:
        ValueError: If the file is not a valid baseline document.

    """
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "results" not in document:
        raise ValueError(f"{path} is not a baseline document: no 'results' key")
    entries = document["results"]
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'results' must be a list")
    baseline: dict[str, BenchmarkResult] = {}
    for entry in entries:
        result = BenchmarkResult.from_dict(entry)
        baseline[result.name] = result
    return baseline


def compare_all(
    results: list[BenchmarkResult],
    baseline: dict[str, BenchmarkResult],
    *,
    tolerance: float = DEFAULT_REGRESSION_TOLERANCE,
) -> list[Comparison]:
    """Compare a whole run against a baseline.

    Args:
        results: Results just produced.
        baseline: Baseline keyed by name.
        tolerance: Fractional p95 increase treated as a regression.

    Returns:
        One comparison per result, in input order.

    """
    return [compare(r, baseline.get(r.name), tolerance=tolerance) for r in results]


def summarise_comparisons(comparisons: list[Comparison]) -> str:
    """Render comparisons as a human-readable table.

    Args:
        comparisons: Comparisons to render.

    Returns:
        A fixed-width table, or a note when there are none.

    """
    if not comparisons:
        return "no comparisons"
    name_width = max(len(c.name) for c in comparisons)
    lines = [
        f"{'benchmark'.ljust(name_width)}  {'verdict':<14} {'baseline p95':>14}  "
        f"{'current p95':>14}  {'change':>10}  note"
    ]
    lines.append("-" * (name_width + 70))
    for item in comparisons:
        baseline_text = f"{item.baseline_p95:.4f}" if item.baseline_p95 is not None else "-"
        current_text = f"{item.current_p95:.4f}" if item.current_p95 is not None else "-"
        if item.relative_change is None:
            change_text = "-"
        else:
            change_text = f"{item.relative_change * _PERCENT * 100:+.1f}%"
        lines.append(
            f"{item.name.ljust(name_width)}  {item.verdict:<14} {baseline_text:>14}  "
            f"{current_text:>14}  {change_text:>10}  {item.reason}"
        )
    return "\n".join(lines)


def is_regression(comparisons: list[Comparison]) -> bool:
    """Return whether any comparison is a regression.

    Args:
        comparisons: Comparisons to inspect.

    Returns:
        ``True`` when at least one verdict is ``regression``.

    """
    return any(item.verdict == "regression" for item in comparisons)


def comparable_scope(baseline: BenchmarkResult, current: BenchmarkResult) -> bool:
    """Return whether two results may be compared directly.

    Args:
        baseline: Previously recorded result.
        current: Result just measured.

    Returns:
        ``True`` when units and measurement scope agree.

    """
    return baseline.units == current.units and baseline.scope is current.scope


#: Re-exported so callers need not import two modules to describe what a scope is.
SCOPE_LIVE_DATABASE: Final[MeasurementScope] = MeasurementScope.LIVE_DATABASE
SCOPE_IN_PROCESS: Final[MeasurementScope] = MeasurementScope.IN_PROCESS
SCOPE_BLOCKED: Final[MeasurementScope] = MeasurementScope.BLOCKED

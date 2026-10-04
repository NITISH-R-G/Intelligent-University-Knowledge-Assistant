"""Benchmark framework: harness, result schema, environment capture, baseline comparison.

Public surface, so a benchmark suite imports from one place:

.. code-block:: python

    from benchmarks.framework import Benchmark, MeasurementScope, run, summarise

The pieces are separable on purpose. ``results`` has no dependency on the harness and can
be used to parse a stored baseline; ``harness`` does not know about files or Git; and
``environment`` is the only module that shells out. A reader trying to work out where a
number came from should be able to follow one file.
"""

from __future__ import annotations

from benchmarks.framework.baseline import (
    DEFAULT_REGRESSION_TOLERANCE,
    Comparison,
    compare,
    compare_all,
    is_regression,
    load_baseline,
    save_baseline,
    summarise_comparisons,
)
from benchmarks.framework.environment import capture_environment
from benchmarks.framework.harness import (
    DEFAULT_ITERATIONS,
    DEFAULT_WARMUP_ITERATIONS,
    Benchmark,
    blocked,
    is_async,
    run,
    run_async,
    summarise,
)
from benchmarks.framework.results import (
    SCHEMA_VERSION,
    BenchmarkResult,
    MeasurementScope,
    Statistics,
    Status,
    compute_statistics,
    median,
    percentile,
)

__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_REGRESSION_TOLERANCE",
    "DEFAULT_WARMUP_ITERATIONS",
    "SCHEMA_VERSION",
    "Benchmark",
    "BenchmarkResult",
    "Comparison",
    "MeasurementScope",
    "Statistics",
    "Status",
    "blocked",
    "capture_environment",
    "compare",
    "compare_all",
    "compute_statistics",
    "is_async",
    "is_regression",
    "load_baseline",
    "median",
    "percentile",
    "run",
    "run_async",
    "save_baseline",
    "summarise",
    "summarise_comparisons",
]

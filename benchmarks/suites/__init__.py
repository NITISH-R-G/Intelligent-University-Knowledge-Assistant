"""Benchmark suites.

One module per subsystem, each exposing ``collect_results``-style runners returning
:class:`~benchmarks.framework.BenchmarkResult`. Suite order is stable so two runs produce
comparable reports.
"""

from __future__ import annotations

__all__: list[str] = []

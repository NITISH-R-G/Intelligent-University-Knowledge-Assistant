"""Phase 1 benchmark suite.

Holds the measurement framework and the benchmarks for code that exists today. See
``docs/12-performance/BENCHMARK_PROTOCOL.md`` for the protocol, the terminology, and -
importantly - what these numbers do and do not claim.

This package is not a test suite. It measures; ``tests/`` asserts. Nothing here fails a
build on its own except through the explicit exit codes in :mod:`benchmarks.run`.
"""

from __future__ import annotations

__all__: list[str] = []

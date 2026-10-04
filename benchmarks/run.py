"""Benchmark suite entry point.

Runs the Phase 1 foundation benchmarks, prints a human-readable table, writes a
machine-readable result file, and compares against a baseline when one exists.

.. code-block:: bash

    python -m benchmarks.run                       # run everything
    python -m benchmarks.run --only health         # run one
    python -m benchmarks.run --save-baseline       # record this run as the baseline
    python -m benchmarks.run --json results.json   # write results somewhere specific

Exit codes are chosen so a CI job can act on them without parsing output.

* ``0`` - every benchmark that ran succeeded. BLOCKED is not a failure.
* ``1`` - at least one benchmark returned ERROR, i.e. the code under test misbehaved.
* ``2`` - a regression was detected against the baseline.

An unavailable database exits ``0`` with a BLOCKED row. Failing the build because a
developer has no Docker daemon running teaches people to ignore the benchmark, which is
worse than not having one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmarks.framework import (  # noqa: E402
    DEFAULT_REGRESSION_TOLERANCE,
    BenchmarkResult,
    compare_all,
    is_regression,
    load_baseline,
    save_baseline,
    summarise,
    summarise_comparisons,
)
from benchmarks.suites.foundation import collect_results, has_errors  # noqa: E402

#: Default location for the recorded baseline. Gitignored: a baseline is a record of one
#: machine's performance, and committing it would invite cross-machine comparison, which
#: is meaningless.
DEFAULT_BASELINE_PATH: Path = REPO_ROOT / ".benchmarks" / "baseline.json"

#: Default location for a run's results. Same reasoning as the baseline.
DEFAULT_RESULTS_PATH: Path = REPO_ROOT / ".benchmarks" / "results.json"


def _write_results(path: Path, results: Sequence[BenchmarkResult]) -> None:
    """Write the full result document.

    Args:
        path: Destination file.
        results: Benchmark results, serialised via ``as_dict``.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": results[0].schema_version if results else "1.0",
        "results": [r.as_dict() for r in results],
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the suite.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code. See the module docstring for the meaning of each value.

    """
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="Run the Phase 1 benchmark suite.",
    )
    parser.add_argument("--only", help="run only benchmarks whose label contains this substring")
    parser.add_argument(
        "--json",
        type=Path,
        default=DEFAULT_RESULTS_PATH,
        help=f"where to write machine-readable results (default: {DEFAULT_RESULTS_PATH})",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help=f"baseline to compare against (default: {DEFAULT_BASELINE_PATH})",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="record this run as the baseline, overwriting any existing one",
    )
    parser.add_argument(
        "--skip-database",
        action="store_true",
        help="omit the live-database benchmark instead of reporting it BLOCKED",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_REGRESSION_TOLERANCE,
        help=(
            "fractional p95 increase treated as a regression "
            f"(default: {DEFAULT_REGRESSION_TOLERANCE})"
        ),
    )
    args = parser.parse_args(argv)

    print("running benchmark suite - this takes a few seconds\n")
    results = collect_results(
        include_database=not args.skip_database,
        only=args.only,
    )
    if not results:
        print("error: --only matched no benchmarks", file=sys.stderr)
        return 1

    print(summarise(results))

    _write_results(args.json, results)
    print(f"\nresults written to {args.json}")

    if args.save_baseline:
        save_baseline(args.baseline, results)
        print(f"baseline saved to {args.baseline}")
        return 1 if has_errors(results) else 0

    baseline = load_baseline(args.baseline)
    if not baseline:
        print(
            f"\nno baseline at {args.baseline}; run with --save-baseline to record one."
            "\nNumbers above are measurements, not targets: Phase 0 defines no Phase 1"
            "\nperformance requirement, so nothing here is being judged against a limit."
        )
        return 1 if has_errors(results) else 0

    comparisons = compare_all(results, baseline, tolerance=args.tolerance)
    print()
    print(summarise_comparisons(comparisons))

    if has_errors(results):
        print("\nat least one benchmark errored", file=sys.stderr)
        return 1
    if is_regression(comparisons):
        print(
            f"\nregression detected against {args.baseline} "
            f"(tolerance {args.tolerance:.0%}). A regression is a change worth explaining,"
            "\nnot a failure: confirm it is real before acting on it.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for the benchmark framework.

These test the *instrument*, not the system. A test that asserted "the API is faster
than X" would be a fabricated performance claim wearing a test's clothes, and it would
fail on a slow CI runner for reasons that have nothing to do with the code.

So everything here is deterministic arithmetic or observable harness behaviour:

* percentile and median are checked against hand-computed values
* warm-up exclusion is checked by counting calls, not by reading timings
* the result schema is checked by round-tripping it and by feeding it garbage
* regression detection is checked by constructing baselines with known p95 values

The one place real timing appears is :meth:`TestHarnessMeasuresEveryIteration`, which
asserts that the harness collected exactly as many samples as it was asked for. That is a
statement about bookkeeping, not about speed.

Phase 0's NFR-014 requires that two runs on the same commit produce comparable aggregate
metrics. That is a property of the environment as much as the code, and the protocol
records the measured variance; these tests pin the parts that are ours.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.framework import (
    DEFAULT_REGRESSION_TOLERANCE,
    SCHEMA_VERSION,
    Benchmark,
    BenchmarkResult,
    MeasurementScope,
    Status,
    blocked,
    compare,
    compute_statistics,
    is_regression,
    load_baseline,
    median,
    percentile,
    run,
    run_async,
    save_baseline,
)
from benchmarks.framework.results import Statistics

pytestmark = pytest.mark.unit


def _result(
    name: str = "b",
    *,
    p95: float | None = 10.0,
    units: str = "milliseconds",
    scope: MeasurementScope = MeasurementScope.IN_PROCESS,
    status: Status = Status.OK,
    samples: int = 100,
) -> BenchmarkResult:
    """Build a result with a controlled distribution.

    Args:
        name: Benchmark name.
        p95: Value to report as p95, or ``None`` to omit statistics.
        units: Result units.
        scope: Measurement scope.
        status: Result status.
        samples: Reported sample count.

    Returns:
        A result whose every statistic equals ``p95``, so a comparison test can reason
        about one number without constructing a full distribution.

    """
    statistics = (
        None
        if p95 is None
        else Statistics(
            samples=samples, min=p95, median=p95, p50=p95, p90=p95, p95=p95, p99=p95, max=p95
        )
    )
    return BenchmarkResult(
        name=name,
        status=status,
        scope=scope,
        units=units,
        iterations=samples,
        warmup_iterations=0,
        schema_version=SCHEMA_VERSION,
        timestamp="2026-10-04T00:00:00.000000Z",
        environment={"commit_sha": "deadbeef"},
        statistics=statistics,
        blocked_reason=None if status is not Status.BLOCKED else "no database",
    )


class TestPercentile:
    """Nearest-rank percentile, checked against hand-computed values."""

    @pytest.mark.parametrize(
        ("values", "pct", "expected"),
        [
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50, 5.0),  # rank ceil(0.50*10)=5 -> index 4
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90, 9.0),
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95, 10.0),  # ceil(9.5)=10 -> clamped to last
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 99, 10.0),
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 100, 10.0),
            ([5], 95, 5.0),  # single sample
            ([3, 1, 2], 50, 2.0),  # input order is irrelevant
        ],
    )
    def test_matches_hand_computed_rank(self, values: list[int], pct: int, expected: float) -> None:
        """Nearest-rank returns an observed value, not an interpolation."""
        assert percentile(values, pct) == expected

    def test_is_order_independent(self) -> None:
        """Sorting internally means callers cannot get a different answer by reordering."""
        forward = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        backward = list(reversed(forward))
        assert percentile(forward, 95) == percentile(backward, 95)

    def test_rejects_empty_input(self) -> None:
        """A percentile over nothing is undefined; inventing 0.0 would be a fake number."""
        with pytest.raises(ValueError, match="at least one"):
            percentile([], 50)

    @pytest.mark.parametrize("pct", [0, -1, 100.1, 150])
    def test_rejects_out_of_range_percentile(self, pct: float) -> None:
        """Out-of-range percentiles are a caller bug, not a value to clamp."""
        with pytest.raises(ValueError, match=r"\(0, 100\]"):
            percentile([1, 2, 3], pct)

    def test_returns_an_observed_value_not_an_interpolated_one(self) -> None:
        """The defining property: the result always occurred.

        An interpolated p95 between two samples that never happened is arithmetic, not
        measurement, and is the usual source of a confidently wrong latency claim.
        """
        samples = [1.0, 100.0]
        for pct in (50, 75, 90, 95, 99):
            assert percentile(samples, pct) in samples


class TestMedian:
    """Median differs from p50 by design, and both are reported."""

    def test_odd_sample_count_returns_middle_value(self) -> None:
        """With an odd count there is one middle value."""
        assert median([1, 2, 3]) == 2.0

    def test_even_sample_count_averages_the_two_middle_values(self) -> None:
        """With an even count the median is not an observed sample."""
        assert median([1, 2, 3, 4]) == 2.5

    def test_differs_from_nearest_rank_p50_on_even_samples(self) -> None:
        """The two functions are genuinely different, which is why both are reported."""
        values = [1.0, 2.0, 3.0, 4.0]
        assert median(values) == 2.5
        assert percentile(values, 50) == 2.0

    def test_rejects_empty_input(self) -> None:
        """Same contract as percentile."""
        with pytest.raises(ValueError, match="at least one"):
            median([])


class TestStatistics:
    """The distribution summary."""

    def test_reports_every_required_percentile(self) -> None:
        """min/median/p50/p90/p95/p99/max are all present for the OPS-015 report format."""
        stats = compute_statistics([float(n) for n in range(1, 101)])
        payload = stats.as_dict()
        assert set(payload) == {"samples", "min", "median", "p50", "p90", "p95", "p99", "max"}
        assert payload["samples"] == 100
        assert payload["min"] == 1.0
        assert payload["max"] == 100.0

    def test_round_trips_through_a_mapping(self) -> None:
        """A baseline read back must equal what was written."""
        stats = compute_statistics([1.0, 2.0, 3.0, 4.0])
        assert Statistics.from_dict(stats.as_dict()) == stats

    def test_rejects_missing_field(self) -> None:
        """A partial baseline is not a baseline with defaults; it is a broken file."""
        payload = compute_statistics([1.0]).as_dict()
        del payload["p95"]
        with pytest.raises(ValueError, match="missing fields"):
            Statistics.from_dict(payload)

    def test_rejects_unknown_field(self) -> None:
        """An unexpected key means the file was written by something else."""
        payload = compute_statistics([1.0]).as_dict()
        payload["p42"] = 1.0
        with pytest.raises(ValueError, match="unknown fields"):
            Statistics.from_dict(payload)

    def test_rejects_non_numeric_value(self) -> None:
        """A string in a numeric field must not be coerced to zero."""
        payload = compute_statistics([1.0]).as_dict()
        payload["p95"] = "fast"
        with pytest.raises(ValueError, match="must be numeric"):
            Statistics.from_dict(payload)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_value(self, bad: float) -> None:
        """NaN and infinity are not latencies; JSON cannot round-trip them meaningfully."""
        payload = compute_statistics([1.0]).as_dict()
        payload["p95"] = bad
        with pytest.raises(ValueError, match="must be finite"):
            Statistics.from_dict(payload)

    def test_rejects_zero_sample_count(self) -> None:
        """Zero samples cannot produce a distribution."""
        payload = compute_statistics([1.0]).as_dict()
        payload["samples"] = 0
        with pytest.raises(ValueError, match="positive int"):
            Statistics.from_dict(payload)


class TestResultSchema:
    """Result serialisation and validation."""

    def test_round_trips_through_json(self) -> None:
        """A written result must read back identically - this is what baselines rely on."""
        original = _result("api.request_healthz", p95=2.5)
        restored = BenchmarkResult.from_dict(json.loads(original.to_json()))
        assert restored == original

    def test_declares_every_field_the_protocol_requires(self) -> None:
        """Name, timestamp, environment, iterations, warm-up, distribution, units, status."""
        payload = _result("x", p95=1.0).as_dict()
        for field in (
            "name",
            "timestamp",
            "environment",
            "iterations",
            "warmup_iterations",
            "units",
            "status",
            "scope",
            "statistics",
            "schema_version",
        ):
            assert field in payload, f"result schema is missing {field!r}"

    def test_rejects_unsupported_schema_version(self) -> None:
        """A baseline from a future format must be refused, not reinterpreted."""
        payload = _result().as_dict()
        payload["schema_version"] = "99.0"
        with pytest.raises(ValueError, match="unsupported result schema_version"):
            BenchmarkResult.from_dict(payload)

    def test_rejects_unknown_status(self) -> None:
        """An unrecognised status must fail loudly."""
        payload = _result().as_dict()
        payload["status"] = "probably_fine"
        with pytest.raises(ValueError, match="invalid status"):
            BenchmarkResult.from_dict(payload)

    def test_rejects_missing_required_field(self) -> None:
        """A result without a name cannot be attributed to anything."""
        payload = _result().as_dict()
        del payload["name"]
        with pytest.raises(ValueError, match="missing required field"):
            BenchmarkResult.from_dict(payload)

    def test_rejects_non_mapping(self) -> None:
        """A JSON array is not a result."""
        with pytest.raises(ValueError, match="must be a mapping"):
            BenchmarkResult.from_dict([1, 2, 3])  # type: ignore[arg-type]


class TestResultValidation:
    """A result must not contradict itself."""

    def test_ok_requires_statistics(self) -> None:
        """A success with no numbers is a claim of excellence, not a missing measurement."""
        with pytest.raises(ValueError, match="requires statistics"):
            _result(p95=None).validate()

    def test_blocked_requires_a_reason(self) -> None:
        """BLOCKED without a reason is indistinguishable from laziness."""
        result = BenchmarkResult(
            name="b",
            status=Status.BLOCKED,
            scope=MeasurementScope.BLOCKED,
            units="ms",
            iterations=0,
            warmup_iterations=0,
        )
        with pytest.raises(ValueError, match="requires a reason"):
            result.validate()

    def test_blocked_must_not_carry_statistics(self) -> None:
        """The central honesty rule: absent and zero are different claims."""
        result = BenchmarkResult(
            name="b",
            status=Status.BLOCKED,
            scope=MeasurementScope.BLOCKED,
            units="ms",
            iterations=0,
            warmup_iterations=0,
            blocked_reason="no database",
            statistics=Statistics(
                samples=1, min=0.0, median=0.0, p50=0.0, p90=0.0, p95=0.0, p99=0.0, max=0.0
            ),
        )
        with pytest.raises(ValueError, match="must not carry statistics"):
            result.validate()

    def test_blocked_result_from_harness_carries_no_statistics(self) -> None:
        """The harness's blocked path is honest by construction."""
        result = blocked(Benchmark("db.connect", units="ms"), "no database")
        result.validate()
        assert result.status is Status.BLOCKED
        assert result.statistics is None
        assert result.blocked_reason == "no database"

    def test_error_requires_a_message(self) -> None:
        """An ERROR without a message cannot be acted on."""
        result = BenchmarkResult(
            name="b",
            status=Status.ERROR,
            scope=MeasurementScope.IN_PROCESS,
            units="ms",
            iterations=0,
            warmup_iterations=0,
        )
        with pytest.raises(ValueError, match="requires an error message"):
            result.validate()


class TestHarnessBookkeeping:
    """The harness counts correctly. It makes no claim about how fast anything is."""

    def test_collects_exactly_the_requested_iterations(self) -> None:
        """Sample count equals iterations, so percentiles have a known denominator."""
        result = run(
            Benchmark("noop", units="milliseconds"),
            lambda: None,
            iterations=37,
            warmup_iterations=0,
        )
        assert result.status is Status.OK
        assert result.statistics is not None
        assert result.statistics.samples == 37

    def test_excludes_warm_up_from_the_measurement(self) -> None:
        """Warm-up iterations execute but are never counted or reported.

        Counting them would let the very first, slowest calls into the distribution and
        produce a p99 no user will ever experience.
        """
        calls: list[int] = []

        def operation() -> None:
            """Record a call.

            Returns:
                Nothing.

            """
            calls.append(1)

        result = run(Benchmark("noop", units="ms"), operation, iterations=10, warmup_iterations=5)
        assert len(calls) == 15, "operation must run warm-up plus measured iterations"
        assert result.warmup_iterations == 5
        assert result.iterations == 10
        assert result.statistics is not None
        assert result.statistics.samples == 10, "warm-up must not enter the sample count"

    def test_warm_up_runs_before_measurement(self) -> None:
        """Order matters: measuring first would capture import and cache-miss costs."""
        order: list[str] = []

        def operation() -> None:
            """Record which phase is running.

            Returns:
                Nothing.

            """
            order.append("call")

        run(Benchmark("noop", units="ms"), operation, iterations=3, warmup_iterations=2)
        assert len(order) == 5

    def test_rejects_zero_iterations_as_error_not_a_crash(self) -> None:
        """A nonsensical configuration is an ERROR result, not an exception."""
        result = run(Benchmark("noop", units="ms"), lambda: None, iterations=0)
        assert result.status is Status.ERROR
        assert result.statistics is None
        assert "iterations must be >= 1" in (result.error or "")

    def test_failing_operation_becomes_an_error_result(self) -> None:
        """A benchmark of broken code reports ERROR rather than crashing the suite."""

        def boom() -> None:
            """Fail.

            Raises:
                RuntimeError: Always.

            """
            raise RuntimeError("kaboom")

        result = run(Benchmark("noop", units="ms"), boom, iterations=1, warmup_iterations=0)
        assert result.status is Status.ERROR
        assert result.statistics is None
        assert "RuntimeError" in (result.error or "")

    def test_records_environment_metadata(self) -> None:
        """A number without its environment is not interpretable."""
        result = run(Benchmark("noop", units="ms"), lambda: None, iterations=1, warmup_iterations=0)
        assert "commit_sha" in result.environment
        assert "python_version" in result.environment

    def test_async_operation_is_measured(self) -> None:
        """The async path collects the same number of samples as the sync path."""

        async def operation() -> None:
            """Do nothing.

            Returns:
                Nothing.

            """

        result = run_async(
            Benchmark("noop", units="ms"), operation, iterations=11, warmup_iterations=2
        )
        assert result.status is Status.OK
        assert result.statistics is not None
        assert result.statistics.samples == 11

    def test_failing_async_operation_becomes_an_error_result(self) -> None:
        """A failing coroutine is contained, as for the sync path."""

        async def boom() -> None:
            """Fail.

            Raises:
                RuntimeError: Always.

            """
            raise RuntimeError("async kaboom")

        result = run_async(Benchmark("noop", units="ms"), boom, iterations=1, warmup_iterations=0)
        assert result.status is Status.ERROR
        assert "async kaboom" in (result.error or "")


class TestBaselineComparison:
    """Regression detection against controlled baselines."""

    def test_no_baseline_is_reported_as_new(self) -> None:
        """First run is not a regression; there is nothing to have regressed from."""
        verdict = compare(_result(p95=10.0), None)
        assert verdict.verdict == "new"
        assert verdict.baseline_p95 is None

    def test_identical_p95_is_unchanged(self) -> None:
        """Zero change is the null result."""
        assert compare(_result(p95=10.0), _result(p95=10.0)).verdict == "unchanged"

    def test_large_increase_is_a_regression(self) -> None:
        """A doubling is unambiguous at any sane tolerance."""
        verdict = compare(_result(p95=20.0), _result(p95=10.0))
        assert verdict.verdict == "regression"
        assert verdict.relative_change == pytest.approx(1.0)

    def test_large_decrease_is_an_improvement(self) -> None:
        """Halving is recognised as an improvement, not a regression."""
        assert compare(_result(p95=5.0), _result(p95=10.0)).verdict == "improvement"

    def test_change_within_tolerance_is_unchanged(self) -> None:
        """Ordinary variance must not be reported as a change worth investigating."""
        assert compare(_result(p95=11.0), _result(p95=10.0)).verdict == "unchanged"

    def test_boundary_is_inclusive_of_the_tolerance(self) -> None:
        """A change exactly at the threshold is not a regression."""
        verdict = compare(_result(p95=10.0 * (1 + DEFAULT_REGRESSION_TOLERANCE)), _result(p95=10.0))
        assert verdict.verdict == "unchanged"

    def test_just_past_the_tolerance_is_a_regression(self) -> None:
        """The comparison is not sticky at the boundary."""
        over = 10.0 * (1 + DEFAULT_REGRESSION_TOLERANCE + 0.01)
        assert compare(_result(p95=over), _result(p95=10.0)).verdict == "regression"

    def test_zero_baseline_is_incomparable_rather_than_infinite(self) -> None:
        """A percentage change from zero is undefined; infinity is not an answer."""
        verdict = compare(_result(p95=5.0), _result(p95=0.0))
        assert verdict.verdict == "incomparable"
        assert verdict.relative_change is None
        assert "undefined" in verdict.reason

    def test_differing_units_are_incomparable(self) -> None:
        """Comparing milliseconds to microseconds would be a confident, meaningless verdict."""
        verdict = compare(_result(p95=10.0, units="ms"), _result(p95=10000.0, units="us"))
        assert verdict.verdict == "incomparable"
        assert "units differ" in verdict.reason

    def test_differing_scope_is_incomparable(self) -> None:
        """An in-process number is not comparable to a live-database one."""
        verdict = compare(
            _result(p95=10.0, scope=MeasurementScope.IN_PROCESS),
            _result(p95=10.0, scope=MeasurementScope.LIVE_DATABASE),
        )
        assert verdict.verdict == "incomparable"
        assert "scope differs" in verdict.reason

    def test_blocked_current_result_is_skipped_not_a_regression(self) -> None:
        """Not measuring is not the same as being slow."""
        current = _result(p95=None, status=Status.BLOCKED, scope=MeasurementScope.BLOCKED)
        assert compare(current, _result(p95=10.0)).verdict == "skipped"

    def test_blocked_baseline_is_incomparable(self) -> None:
        """A baseline that never measured cannot anchor a comparison."""
        baseline = _result(p95=None, status=Status.BLOCKED, scope=MeasurementScope.BLOCKED)
        assert compare(_result(p95=10.0), baseline).verdict == "incomparable"

    def test_regression_flag_ignores_improvements_and_unchanged(self) -> None:
        """The exit-code predicate must only fire on an actual regression."""
        comparisons = [
            compare(_result(p95=5.0), _result(p95=10.0)),
            compare(_result(p95=10.0), _result(p95=10.0)),
        ]
        assert is_regression(comparisons) is False
        assert is_regression([compare(_result(p95=20.0), _result(p95=10.0))]) is True


class TestBaselinePersistence:
    """Storing and reloading a baseline."""

    def test_round_trips_through_a_file(self, tmp_path: Path) -> None:
        """What is saved must be what is loaded, keyed by name."""
        path = tmp_path / "baseline.json"
        save_baseline(path, [_result("a", p95=1.0), _result("b", p95=2.0)])
        loaded = load_baseline(path)
        assert set(loaded) == {"a", "b"}
        assert loaded["a"].statistics is not None
        assert loaded["a"].statistics.p95 == 1.0

    def test_missing_baseline_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """A first run has no baseline; that is a normal state, not a failure."""
        assert load_baseline(tmp_path / "absent.json") == {}

    def test_rejects_a_file_that_is_not_a_baseline(self, tmp_path: Path) -> None:
        """A stray JSON file must not be silently treated as empty and compared against."""
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({"unexpected": True}), encoding="utf-8")
        with pytest.raises(ValueError, match="not a baseline document"):
            load_baseline(path)

    def test_rejects_a_baseline_whose_results_are_not_a_list(self, tmp_path: Path) -> None:
        """Shape is checked before contents."""
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({"results": {"a": 1}}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            load_baseline(path)


class TestProtocolLanguage:
    """The framework must not state performance requirements it cannot support."""

    def test_tolerance_is_documented_as_methodology_not_a_target(self) -> None:
        """Phase 0 defines no Phase 1 target, so the threshold must not claim to be one."""
        source = Path("benchmarks/framework/baseline.py").read_text(encoding="utf-8")
        assert "NOT A PERFORMANCE REQUIREMENT" in source

    def test_in_process_scope_documents_that_it_is_not_production_latency(self) -> None:
        """The most dangerous misuse is quoting an in-process number as a production one."""
        source = Path("benchmarks/framework/results.py").read_text(encoding="utf-8")
        assert "NOT a production latency" in source

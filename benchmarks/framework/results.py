"""Result schema for the benchmark framework.

One dataclass and its JSON round-trip. The schema is deliberately explicit rather than
derived: a benchmark result outlives the run that produced it, gets pasted into pull
requests and issue trackers, and is compared against baselines months later. A result
whose meaning is implicit in the code that wrote it is unreadable by then.

Two rules govern everything here.

**A measurement is never invented.** Every field that describes a timing is populated
from real samples. If a benchmark cannot run, the result carries
``Status.BLOCKED`` and a reason; the statistics are absent rather than zero, because a
zero millisecond latency is a false claim that reads like an excellent result.

**A number is never silently a performance requirement.** ``Scope`` records what a
number describes. ``IN_PROCESS`` timings exercise real application code with no
external boundary, which is useful for catching regressions but is not a statement about
production latency. Stating that on every result is the difference between a benchmark
suite and a source of confident misinformation.

See ``docs/12-performance/BENCHMARK_PROTOCOL.md`` for the protocol these results follow.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
from collections.abc import Sequence
from typing import Any, Final

__all__ = [
    "MeasurementScope",
    "Statistics",
    "Status",
    "BenchmarkResult",
    "SCHEMA_VERSION",
    "compute_statistics",
    "median",
    "percentile",
]

#: Bumped when the JSON shape changes incompatibly. A baseline recorded under a different
#: version must be rejected rather than silently compared field by field.
SCHEMA_VERSION: Final[str] = "1.0"

#: Percentiles reported for every successful benchmark, as required by the protocol.
#: p90 is included alongside the OPS-015 trio of p50/p95/p99 because a regression
#: usually shows in the p90 before it reaches p99.
REPORTED_PERCENTILES: Final[tuple[int, ...]] = (50, 90, 95, 99)

#: A percentile is expressed in (0, 100]; dividing by this turns it into a fraction of
#: the sample count.
_PERCENTILE_SCALE: Final[float] = 100.0


class Status(enum.StrEnum):
    """Whether a benchmark produced a measurement."""

    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"


class MeasurementScope(enum.StrEnum):
    """What a measurement actually covers.

    This field exists because the most dangerous outcome of a benchmark suite is a
    number that is true of the harness and false of the system.
    """

    #: Real application code, no external I/O boundary: no network socket, no database.
    #: Catches regressions in our own logic. NOT a production latency claim - a number
    #: measured here excludes every cost the production path actually pays.
    IN_PROCESS = "in_process"
    #: Real PostgreSQL over a real connection. Absent from Phase 1: see the protocol's
    #: "Known limitations".
    LIVE_DATABASE = "live_database"
    #: Not measured, and the reason is recorded. Never accompanied by statistics.
    BLOCKED = "blocked"


def percentile(values: Sequence[float], pct: float) -> float:
    """Return the nearest-rank percentile of ``values``.

    Nearest-rank rather than interpolated: it always returns a value that was actually
    observed, which is the property a reader needs from a latency number. An
    interpolated p95 of 3.7 ms between two samples that never occurred is arithmetic,
    not measurement.

    Determinism matters more than statistical elegance here. Phase 0's NFR-014 requires
    that two runs on the same commit produce comparable aggregates, and a
    method that depends on sample positions rather than values is one fewer thing that
    can differ between machines.

    Args:
        values: Observed values. Order is irrelevant.
        pct: Percentile in (0, 100].

    Returns:
        The value at the nearest rank.

    Raises:
        ValueError: If ``values`` is empty or ``pct`` is outside (0, 100].

    """
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 < pct <= _PERCENTILE_SCALE:
        raise ValueError(f"percentile must be in (0, 100], got {pct}")
    ordered = sorted(values)
    # ceil(pct/100 * N), clamped so an index of exactly N wraps to the last element.
    rank = math.ceil(pct / _PERCENTILE_SCALE * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


def median(values: Sequence[float]) -> float:
    """Return the statistical median: the mean of the two central values for even ``N``.

    Reported separately from p50 because the protocol asks for both, and they are not the
    same function: for an even sample count the median averages the two middle samples
    while p50 (nearest-rank) returns the lower of them. Reporting both makes the
    convention visible instead of leaving a reader to guess which one a number is.

    Args:
        values: Observed values. Order is irrelevant.

    Returns:
        The median.

    Raises:
        ValueError: If ``values`` is empty.

    """
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


@dataclasses.dataclass(frozen=True, slots=True)
class Statistics:
    """Latency distribution for one benchmark, in the benchmark's declared units."""

    samples: int
    min: float
    median: float
    p50: float
    p90: float
    p95: float
    p99: float
    max: float

    def as_dict(self) -> dict[str, float | int]:
        """Return the distribution as a JSON-serialisable mapping.

        Returns:
            Field name to value, in a stable order.

        """
        return {
            "samples": self.samples,
            "min": self.min,
            "median": self.median,
            "p50": self.p50,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "max": self.max,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Statistics:
        """Rebuild statistics from a mapping, rejecting anything malformed.

        Used to read baselines, where a silently accepted bad number would be compared
        against real measurements and produce a meaningless regression verdict.

        Args:
            payload: Mapping produced by :meth:`as_dict`.

        Returns:
            The parsed statistics.

        Raises:
            ValueError: If a field is missing, of the wrong type, or not finite.

        """
        required = {"samples", "min", "median", "p50", "p90", "p95", "p99", "max"}
        missing = required - set(payload)
        if missing:
            raise ValueError(f"statistics missing fields: {sorted(missing)}")
        extra = set(payload) - required
        if extra:
            raise ValueError(f"statistics has unknown fields: {sorted(extra)}")
        samples = payload["samples"]
        if not isinstance(samples, int) or isinstance(samples, bool) or samples < 1:
            raise ValueError(f"statistics samples must be a positive int, got {samples!r}")
        numbers: dict[str, float] = {}
        for field in sorted(required - {"samples"}):
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"statistics {field} must be numeric, got {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"statistics {field} must be finite, got {value!r}")
            numbers[field] = float(value)
        return cls(samples=samples, **numbers)


def compute_statistics(values: Sequence[float]) -> Statistics:
    """Summarise observed values.

    Args:
        values: Observed values, in the benchmark's units.

    Returns:
        The distribution.

    Raises:
        ValueError: If ``values`` is empty.

    """
    if not values:
        raise ValueError("compute_statistics requires at least one value")
    distribution = {f"p{p}": percentile(values, p) for p in REPORTED_PERCENTILES}
    return Statistics(
        samples=len(values),
        min=min(values),
        median=median(values),
        max=max(values),
        **distribution,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """One benchmark's outcome.

    Exactly one of ``statistics`` / ``blocked_reason`` is meaningful, and ``status``
    says which. Constructing an inconsistent pair is not prevented by the type system,
    so :meth:`validate` re-checks it wherever a result arrives from outside - a JSON file,
    a baseline written by an earlier run.
    """

    name: str
    status: Status
    scope: MeasurementScope
    units: str
    iterations: int
    warmup_iterations: int
    schema_version: str = SCHEMA_VERSION
    timestamp: str = ""
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    statistics: Statistics | None = None
    blocked_reason: str | None = None
    error: str | None = None
    notes: str = ""

    def validate(self) -> None:
        """Assert internal consistency.

        Raises:
            ValueError: If the status, scope, statistics and reason contradict each other.

        """
        if self.status is Status.OK:
            if self.statistics is None:
                raise ValueError(f"{self.name}: status=ok requires statistics")
            if self.scope is MeasurementScope.BLOCKED:
                raise ValueError(f"{self.name}: status=ok cannot have scope=blocked")
            if self.blocked_reason:
                raise ValueError(f"{self.name}: status=ok cannot carry a blocked_reason")
        if self.status is Status.BLOCKED:
            if self.scope is not MeasurementScope.BLOCKED:
                raise ValueError(f"{self.name}: status=blocked requires scope=blocked")
            if not self.blocked_reason:
                raise ValueError(f"{self.name}: status=blocked requires a reason")
            if self.statistics is not None:
                raise ValueError(
                    f"{self.name}: status=blocked must not carry statistics - "
                    "an absent measurement and a zero are different claims"
                )
        if self.status is Status.ERROR and not self.error:
            raise ValueError(f"{self.name}: status=error requires an error message")

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable representation.

        Returns:
            A mapping suitable for ``json.dump``. Stable key order for readable diffs.

        """
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "status": str(self.status),
            "scope": str(self.scope),
            "units": self.units,
            "iterations": self.iterations,
            "warmup_iterations": self.warmup_iterations,
            "timestamp": self.timestamp,
            "environment": dict(self.environment),
        }
        if self.statistics is not None:
            payload["statistics"] = self.statistics.as_dict()
        if self.blocked_reason is not None:
            payload["blocked_reason"] = self.blocked_reason
        if self.error is not None:
            payload["error"] = self.error
        if self.notes:
            payload["notes"] = self.notes
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        """Render the result as JSON.

        Args:
            indent: Passed to :func:`json.dumps`.

        Returns:
            The JSON document.

        """
        self.validate()
        return json.dumps(self.as_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BenchmarkResult:
        """Rebuild a result from JSON, rejecting anything malformed.

        Args:
            payload: Mapping produced by :meth:`as_dict`.

        Returns:
            The parsed result, guaranteed to have passed :meth:`validate`.

        Raises:
            ValueError: If the payload is not a well-formed benchmark result.

        """
        if not isinstance(payload, dict):
            raise ValueError(f"benchmark result must be a mapping, got {type(payload).__name__}")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported result schema_version {version!r}; expected {SCHEMA_VERSION!r}"
            )
        for field in ("name", "status", "scope", "units", "timestamp"):
            if field not in payload:
                raise ValueError(f"benchmark result missing required field {field!r}")

        def _as_enum(field: str, enum_cls: type[enum.StrEnum]) -> Any:
            raw = payload[field]
            try:
                return enum_cls(raw)
            except ValueError as exc:
                allowed = sorted(member.value for member in enum_cls)
                raise ValueError(f"invalid {field}={raw!r}; expected one of {allowed}") from exc

        statistics_payload = payload.get("statistics")
        statistics = (
            Statistics.from_dict(statistics_payload) if statistics_payload is not None else None
        )
        environment = payload.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError("environment must be a mapping")

        result = cls(
            name=payload["name"],
            status=_as_enum("status", Status),
            scope=_as_enum("scope", MeasurementScope),
            units=payload["units"],
            iterations=int(payload.get("iterations", 0)),
            warmup_iterations=int(payload.get("warmup_iterations", 0)),
            schema_version=version,
            timestamp=payload["timestamp"],
            environment={str(k): str(v) for k, v in environment.items()},
            statistics=statistics,
            blocked_reason=payload.get("blocked_reason"),
            error=payload.get("error"),
            notes=payload.get("notes", ""),
        )
        result.validate()
        return result

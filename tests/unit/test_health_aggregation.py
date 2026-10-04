"""Unit tests for liveness/readiness aggregation.

The single most expensive Phase 1 mistake is a readiness probe that lies. A readiness
endpoint that returns 200 while the database is unreachable keeps the load balancer sending
traffic into a process that will fail every request - converting a dependency outage into a
total outage. These tests pin the three aggregation rules and, just as importantly, pin that a
failing dependency never leaks its own error text.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from knowledge_assistant.application.health import HealthService
from knowledge_assistant.domain.clock import UTC, FixedClock
from knowledge_assistant.domain.health import (
    ProbeCriticality,
    ProbeResult,
    ProbeStatus,
    aggregate,
    safe_probe_detail,
)

from ..conftest import FakeProbe, make_health_service

pytestmark = pytest.mark.unit


def _result(
    name: str,
    status: ProbeStatus = ProbeStatus.OK,
    criticality: ProbeCriticality = ProbeCriticality.CRITICAL,
) -> ProbeResult:
    """Build a probe result for aggregation tests.

    Args:
        name: Probe name.
        status: Probe status.
        criticality: Probe criticality.

    Returns:
        A ``ProbeResult``.

    """
    return ProbeResult(
        name=name,
        status=status,
        criticality=criticality,
        detail="ok",
        checked_at=dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
    )


class TestAggregationRules:
    """The three documented rules, each with the failure it prevents."""

    def test_all_ok_is_ready(self) -> None:
        assert aggregate([_result("postgres")], now=dt.datetime.now(tz=UTC)).ready is True

    def test_critical_failure_blocks_readiness(self) -> None:
        """A dead database must take the instance out of rotation, not return 200."""
        report = aggregate([_result("postgres", ProbeStatus.FAILED)], now=dt.datetime.now(tz=UTC))
        assert report.ready is False
        assert report.degraded is False

    def test_critical_degraded_also_blocks_readiness(self) -> None:
        """Serving traffic onto a degraded critical dependency converts a partial failure
        into user-visible errors, so DEGRADED is treated as not-ready for CRITICAL probes.
        """
        report = aggregate([_result("postgres", ProbeStatus.DEGRADED)], now=dt.datetime.now(tz=UTC))
        assert report.ready is False

    def test_advisory_failure_degrades_without_blocking(self) -> None:
        """A degraded advisory dependency must not pull the instance out of rotation:
        the service can still serve, and removing it reduces capacity for no benefit.
        """
        report = aggregate(
            [_result("object_store", ProbeStatus.FAILED, ProbeCriticality.ADVISORY)],
            now=dt.datetime.now(tz=UTC),
        )
        assert report.ready is True
        assert report.degraded is True

    def test_advisory_failure_with_healthy_critical_is_ready_and_degraded(self) -> None:
        report = aggregate(
            [
                _result("postgres"),
                _result("model_runtime", ProbeStatus.DEGRADED, ProbeCriticality.ADVISORY),
            ],
            now=dt.datetime.now(tz=UTC),
        )
        assert (report.ready, report.degraded) == (True, True)

    def test_empty_probe_set_is_ready(self) -> None:
        """A service with no declared dependencies legitimately has no probes; refusing
        readiness would make it permanently unrouteable.
        """
        assert aggregate([], now=dt.datetime.now(tz=UTC)).ready is True

    def test_probe_order_is_preserved(self) -> None:
        """Stable output order keeps the readiness payload diffable between releases."""
        report = aggregate([_result("b"), _result("a"), _result("c")], now=dt.datetime.now(tz=UTC))
        assert [p.name for p in report.probes] == ["b", "a", "c"]


class TestSafeProbeDetail:
    """Readiness is unauthenticated, so probe failure detail is the classic leak."""

    @pytest.mark.parametrize(
        ("exc_type", "message", "secrets"),
        [
            (
                type("OperationalError", (Exception,), {}),
                r'connection to server at "db.internal" (10.0.0.7) failed: '
                r'password authentication failed for user "app"',
                ("password", "10.0.0.7", "db.internal", "app"),
            ),
            (
                type("InterfaceError", (Exception,), {}),
                'could not connect to server: FATAL: database "ka_prod" does not exist',
                ("ka_prod", "FATAL"),
            ),
            (
                RuntimeError,
                "SELECT * FROM jobs -- leaked sql",
                ("SELECT", "jobs"),
            ),
        ],
    )
    def test_message_never_appears(
        self, exc_type: type[BaseException], message: str, secrets: tuple[str, ...]
    ) -> None:
        """A driver exception embeds host, port, user, database and sometimes SQL. Only the
        exception class name is emitted.
        """
        detail = safe_probe_detail(exc_type(message))
        assert detail == f"failed:{exc_type.__name__.lower()}"
        for secret in secrets:
            assert secret not in detail

    def test_unnamed_exception_class_is_safe(self) -> None:
        """The fallback must not produce an empty or exception-leaking token."""
        assert safe_probe_detail(RuntimeError()).startswith("failed:")


class TestHealthService:
    """Orchestration behaviour: concurrency, exception containment and the time budget."""

    async def test_all_ok_reports_ready(self) -> None:
        clock = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        service = make_health_service([FakeProbe("postgres"), FakeProbe("queue")], clock)
        report = await service.report()
        assert report.ready is True
        assert [p.name for p in report.probes] == ["postgres", "queue"]

    async def test_probe_exception_becomes_failed_result(self) -> None:
        """A probe that raises must not take down the health endpoint itself - otherwise the
        orchestrator sees a connection error instead of a 503, and the real cause is invisible.
        """
        clock = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        service = make_health_service(
            [FakeProbe("postgres", raises=RuntimeError("password=hunter2"))], clock
        )
        report = await service.report()
        assert report.ready is False
        detail = report.probes[0].detail or ""
        assert detail.startswith("failed:")
        assert "hunter2" not in detail

    async def test_probe_set_timeout_marks_not_ready(self) -> None:
        """A hung dependency must surface as not-ready within the budget, not as a hung
        health endpoint: an orchestrator that cannot get an answer eventually kills a healthy
        process and restarts it, which turns a slow database into a crash loop.
        """
        clock = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        service = HealthService(
            [FakeProbe("slow", delay_seconds=0.5)], clock=clock, timeout_seconds=0.05
        )
        report = await service.report()
        assert report.ready is False
        assert "budget" in (report.probes[0].detail or "")

    async def test_probes_run_concurrently_not_in_series(self) -> None:
        """Series execution would make the readiness budget the sum of all probe latencies.
        Five probes at 100 ms each would be a 500 ms health check.
        """
        clock = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        probes = [FakeProbe(f"p{i}", delay_seconds=0.1) for i in range(5)]
        service = HealthService(probes, clock=clock, timeout_seconds=2.0)
        started = asyncio.get_running_loop().time()
        await service.report()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 0.3, f"probes appear serialised: {elapsed:.3f}s"

    async def test_advisory_degradation_surfaces_in_report(self) -> None:
        clock = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        service = make_health_service(
            [
                FakeProbe("postgres"),
                FakeProbe(
                    "object_store",
                    status=ProbeStatus.DEGRADED,
                    criticality=ProbeCriticality.ADVISORY,
                ),
            ],
            clock,
        )
        report = await service.report()
        assert (report.ready, report.degraded) == (True, True)

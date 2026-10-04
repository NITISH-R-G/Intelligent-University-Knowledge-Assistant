"""Failure injection: liveness, readiness and probe timeouts.

Covers Phase 0 **OPS-001** ("liveness does not check dependencies; readiness fails when
retrieval is unusable") and **OPS-006** ("timeouts on every outbound call ... verified by
fault injection").

The distinction is the whole point of the two endpoints, and it is easy to get subtly
wrong in both directions:

* if liveness checks dependencies, a database blip makes every API process look dead and
  a supervisor restarts the entire fleet - turning a recoverable dependency failure into a
  crash loop;
* if readiness does not check dependencies, traffic is routed to a process whose every
  request fails, and the failure becomes user-visible instead of a removed instance.

Neither is hypothetical. Each test below states which catastrophe it prevents.
"""

from __future__ import annotations

import asyncio

import pytest

from knowledge_assistant.domain.errors import DependencyUnavailableError
from knowledge_assistant.domain.health import ProbeCriticality, ProbeStatus

from ..conftest import TEST_EPOCH, FakeProbe, make_app, make_health_service

pytestmark = pytest.mark.failure_injection


async def _client(probes: list[FakeProbe], **app_kwargs: object) -> object:
    """Return a client over an app whose readiness is backed by ``probes``.

    Args:
        probes: Probe doubles backing readiness.
        **app_kwargs: Overrides passed to ``make_app``.

    Returns:
        An ``httpx.AsyncClient`` over an ASGI transport.

    """
    import httpx  # noqa: PLC0415

    from knowledge_assistant.config.settings import Settings  # noqa: PLC0415
    from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

    clock = FixedClock(TEST_EPOCH)
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        database_url="postgresql://t:t@localhost/ka",
        service_name="ka-failure",
    )
    app = make_app(probes=list(probes), clock=clock, settings=settings, **app_kwargs)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestLivenessSurvivesDependencyFailure:
    """OPS-001: liveness must not depend on PostgreSQL."""

    async def test_healthz_stays_200_when_a_critical_dependency_is_down(self) -> None:
        """The catastrophic-failure guard.

        If this ever fails, a database outage causes every API process to report itself
        dead, and whatever supervises them restarts the fleet - converting one recoverable
        dependency failure into a full outage that also destroys in-flight work.
        """
        down = FakeProbe(
            "postgres",
            status=ProbeStatus.FAILED,
            criticality=ProbeCriticality.CRITICAL,
            detail="connection refused",
        )
        async with await _client([down]) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200

    async def test_healthz_stays_200_when_a_probe_raises(self) -> None:
        """An exploding dependency must not take the liveness endpoint with it."""
        exploding = FakeProbe(
            "postgres", criticality=ProbeCriticality.CRITICAL, raises=RuntimeError("boom")
        )
        async with await _client([exploding]) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200


class TestReadinessReflectsDependencyFailure:
    """OPS-001: readiness must fail when a critical dependency is unusable."""

    async def test_readyz_is_503_when_a_critical_probe_fails(self) -> None:
        """Traffic must stop reaching a process whose database is unreachable."""
        down = FakeProbe(
            "postgres",
            status=ProbeStatus.FAILED,
            criticality=ProbeCriticality.CRITICAL,
            detail="connection refused",
        )
        async with await _client([down]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["ready"] is False

    async def test_readyz_is_503_when_a_critical_probe_raises(self) -> None:
        """An exception during a probe is a failed probe, not a crashed endpoint."""
        exploding = FakeProbe(
            "postgres", criticality=ProbeCriticality.CRITICAL, raises=RuntimeError("boom")
        )
        async with await _client([exploding]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["ready"] is False

    async def test_readyz_is_503_when_a_critical_probe_is_degraded(self) -> None:
        """A *degraded* critical dependency is treated as not-ready.

        Serving traffic onto a degraded dependency converts a partial failure into
        user-visible errors, which is strictly worse than removing the instance.
        """
        degraded = FakeProbe(
            "postgres",
            status=ProbeStatus.DEGRADED,
            criticality=ProbeCriticality.CRITICAL,
            detail="pool exhausted",
        )
        async with await _client([degraded]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503

    async def test_a_raised_dependency_error_does_not_disclose_the_dsn(self) -> None:
        """The readiness body is unauthenticated; driver detail must not appear in it.

        The realistic leak vector is a probe *raising* a driver error whose message
        embeds the connection string. Note the scope of the guarantee: it covers raised
        exceptions, which are attacker-uncontrolled. A probe that *returns* a detail
        string is trusted first-party code and its detail is passed through - the real
        ``DatabaseProbe`` never returns a driver message, it lets the exception
        propagate, which is why this path is the one that matters.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        exploding = FakeProbe(
            "postgres",
            criticality=ProbeCriticality.CRITICAL,
            raises=RuntimeError("connection to 10.0.0.5 failed: password=hunter2"),
        )
        service = make_health_service([exploding], FixedClock(TEST_EPOCH))

        report = await service.report()

        detail = report.probes[0].detail or ""
        assert "hunter2" not in detail
        assert "10.0.0.5" not in detail

    async def test_readiness_body_omits_the_dependency_error_detail(self) -> None:
        """End to end: the HTTP response body carries no driver detail."""
        exploding = FakeProbe(
            "postgres",
            criticality=ProbeCriticality.CRITICAL,
            raises=RuntimeError("could not connect to 10.0.0.5 password=hunter2"),
        )
        async with await _client([exploding]) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert "hunter2" not in response.text
        assert "10.0.0.5" not in response.text

    async def test_probe_failure_detail_is_a_class_name_not_a_message(self) -> None:
        """A raised exception is reported by type only.

        The message is where connection strings and hostnames live; the type is enough
        for an operator to identify the failure from the dependency name alone.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        exploding = FakeProbe(
            "postgres",
            criticality=ProbeCriticality.CRITICAL,
            raises=DependencyUnavailableError(
                "dsn=postgresql://u:p@10.0.0.5/ka", dependency="postgres"
            ),
        )
        service = make_health_service([exploding], FixedClock(TEST_EPOCH))
        report = await service.report()

        detail = report.probes[0].detail or ""
        assert "hunter2" not in detail
        assert "10.0.0.5" not in detail
        assert "dependencyunavailable" in detail.lower()


class TestAdvisoryFailuresDoNotBlockTraffic:
    """An advisory dependency is visible without taking the instance out of rotation."""

    async def test_readyz_stays_200_but_reports_degraded(self) -> None:
        """The point of criticality separation.

        Object storage and the model runtime are declared but not yet wired in Phase 1.
        If that made the service not-ready, no deployment could ever come up, because the
        dependencies do not exist yet.
        """
        advisory = FakeProbe(
            "object_store",
            status=ProbeStatus.FAILED,
            criticality=ProbeCriticality.ADVISORY,
            detail="not configured",
        )
        async with await _client([advisory]) as client:
            response = await client.get("/readyz")
        body = response.json()
        assert response.status_code == 200
        assert body["ready"] is True
        assert body["degraded"] is True


class TestProbeTimeout:
    """OPS-006: a hung dependency must not hold readiness open indefinitely."""

    async def test_a_slow_probe_is_reported_failed_not_awaited_forever(self) -> None:
        """Scenario: a dependency that never answers.

        Without a timeout, one hung call holds the readiness endpoint open until the client
        gives up, and the process is removed from rotation for the wrong reason - it looks
        slow rather than unhealthy.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        slow = FakeProbe(
            "postgres",
            criticality=ProbeCriticality.CRITICAL,
            delay_seconds=5.0,
        )
        service = make_health_service([slow], FixedClock(TEST_EPOCH), timeout_seconds=0.05)

        report = await asyncio.wait_for(service.report(), timeout=2.0)

        assert report.ready is False
        assert report.probes[0].status is ProbeStatus.FAILED

    async def test_probe_timeout_reports_a_budget_exhaustion(self) -> None:
        """A timeout is reported as an exhausted budget, not as a generic failure.

        The two have different causes and different responses: an exhausted budget usually
        means a saturated pool or a hung call, while a generic failure means the probe
        returned or raised something concrete.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        slow = FakeProbe(
            "postgres",
            criticality=ProbeCriticality.CRITICAL,
            delay_seconds=5.0,
        )
        service = make_health_service([slow], FixedClock(TEST_EPOCH), timeout_seconds=0.05)

        report = await asyncio.wait_for(service.report(), timeout=2.0)

        detail = report.probes[0].detail or ""
        assert "budget" in detail.lower()

    async def test_timeout_fails_every_probe_and_readiness(self) -> None:
        """The documented per-set timeout semantics, asserted deliberately.

        ``HealthService`` bounds the whole probe set, not each probe, so a set timeout marks
        *every* probe FAILED. The alternative - per-probe timeouts - would identify the
        culprit precisely but adds a second timeout mechanism with its own failure modes,
        and the code comments that trade-off explicitly.

        The behaviour worth defending is that a timeout never yields ``ready``: the safe
        direction is to report unhealthy rather than to guess which probe was slow.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        healthy = FakeProbe("object_store", criticality=ProbeCriticality.ADVISORY)
        slow = FakeProbe("postgres", criticality=ProbeCriticality.CRITICAL, delay_seconds=5.0)
        service = make_health_service([healthy, slow], FixedClock(TEST_EPOCH), timeout_seconds=0.05)

        report = await asyncio.wait_for(service.report(), timeout=2.0)

        assert report.ready is False
        assert {p.status for p in report.probes} == {ProbeStatus.FAILED}
        assert len(report.probes) == 2, "every declared probe must still be reported"

    async def test_a_slow_probe_does_not_delay_a_healthy_service(self) -> None:
        """Probes run concurrently, so total time is bounded by the slowest, not the sum.

        A sequential implementation would take slow-plus-fast, and with several
        dependencies the sum would exceed the budget even when no individual call is slow.
        """
        from knowledge_assistant.domain.clock import FixedClock  # noqa: PLC0415

        probes = [
            FakeProbe(f"dep{i}", criticality=ProbeCriticality.ADVISORY, delay_seconds=0.2)
            for i in range(5)
        ]
        service = make_health_service(probes, FixedClock(TEST_EPOCH), timeout_seconds=5.0)

        report = await asyncio.wait_for(service.report(), timeout=2.0)

        assert report.ready is True
        assert all(p.status is ProbeStatus.OK for p in report.probes)


class TestHealthyBaseline:
    """The negative control: none of the above proves anything if health never breaks."""

    async def test_all_healthy_yields_ready_not_degraded(self) -> None:
        """Without this, a service that always reported not-ready would pass every
        failure test above while being completely broken in the other direction.
        """
        healthy = FakeProbe("postgres", criticality=ProbeCriticality.CRITICAL)
        async with await _client([healthy]) as client:
            response = await client.get("/readyz")
        body = response.json()
        assert response.status_code == 200
        assert body["ready"] is True
        assert body["degraded"] is False

    async def test_liveness_and_readiness_agree_when_healthy(self) -> None:
        """The two endpoints differ only under failure."""
        healthy = FakeProbe("postgres", criticality=ProbeCriticality.CRITICAL)
        async with await _client([healthy]) as client:
            liveness = await client.get("/healthz")
            readiness = await client.get("/readyz")
        assert liveness.status_code == 200
        assert readiness.status_code == 200

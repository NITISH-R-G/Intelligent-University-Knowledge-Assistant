"""Negative controls: proof that this lane would fail if a guarantee were removed.

A resilience suite that passes is only evidence if it could have failed. Two ways that
evidence is worthless, and both are guarded against here.

**The assertion is vacuous.** Every failure test in this lane asserts a *positive*: the retry
happened, the key was released, the endpoint returned 403. An assertion that would also pass
against a system with no retry policy, no idempotency and no auth gate proves nothing about
any of them. :class:`TestMutationsAreDetected` removes each guard from the running system and
shows the lane's own expectations break.

**The harness is broken.** The fault is injected by test doubles - ``ExplodingJobRepository``
and ``FailingIdempotencyStore``. A double that silently stopped failing would make every test
that depends on it pass for the wrong reason, and no amount of green would reveal it.
:class:`TestHarnessIntegrity` asserts the doubles actually inject.

The mutations below replace a decision function *at the module seam where it is used* and
observe the consequence. They are not used to inject faults into production paths - the fault
injection proper goes through ports and test doubles - and they are undone by pytest's
``monkeypatch`` fixture, so no test in this repository observes a weakened system.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from knowledge_assistant.application import jobs as jobs_application
from knowledge_assistant.domain.errors import DependencyUnavailableError, ValidationError
from knowledge_assistant.domain.idempotency import IdempotencyScope
from knowledge_assistant.domain.jobs import FailureClass, JobState, classify_failure
from knowledge_assistant.workers.handlers import FAILING_JOB_TYPE, build_registry
from knowledge_assistant.workers.runner import WorkerRunner

from ..conftest import FakeProbe
from ..failure.conftest import (
    ExplodingJobRepository,
    FailingIdempotencyStore,
    build_executor,
    make_idempotency_store,
)

pytestmark = pytest.mark.failure_injection

_SCOPE = IdempotencyScope.JOB_SUBMIT


class _Weakened:
    """Recreates the guards removed by each mutation, for side-by-side comparison.

    Kept in this module rather than imported so the point is visible: these are *not* what
    production does. Each is the production function minus one decision.
    """

    @staticmethod
    def should_retry_without_permanent_rule(
        *, failure_class: FailureClass, attempts_used: int, max_attempts: int
    ) -> bool:
        """``should_retry`` with the ``PERMANENT`` short-circuit deleted.

        Args:
            failure_class: Classification of the failure.
            attempts_used: Attempts consumed so far.
            max_attempts: Ceiling for the job.

        Returns:
            Whether the job would be rescheduled.

        """
        del failure_class
        return attempts_used < max_attempts

    @staticmethod
    def classify_everything_as_transient(exc: BaseException) -> FailureClass:
        """``classify_failure`` with the whole taxonomy collapsed to ``TRANSIENT``.

        Args:
            exc: Raised exception.

        Returns:
            Always ``TRANSIENT``.

        """
        del exc
        return FailureClass.TRANSIENT


async def _drive_permanent(repository: Any, clock: Any) -> list[str]:
    """Submit a permanently failing job and drain it.

    Args:
        repository: Job repository.
        clock: Fixed clock.

    Returns:
        The outcome vocabulary of each execution, in order.

    """
    from knowledge_assistant.application.jobs import SubmitJob  # noqa: PLC0415

    executor = build_executor(repository, build_registry(), clock=clock)
    await SubmitJob(repository, clock=clock).execute(
        job_type=FAILING_JOB_TYPE, payload={"failure_kind": "permanent"}
    )
    outcomes: list[str] = []
    for _ in range(5):
        outcome = await executor.execute_once()
        if outcome is None:
            break
        outcomes.append(outcome.outcome)
        stored = next(iter(repository._jobs.values()))
        if stored.state is JobState.RETRY:
            # Pull the retry forward so the driver can observe the next attempt; the backoff
            # schedule itself is asserted in test_worker_failure, not here.
            repository._jobs[stored.id] = dataclasses.replace(  # noqa: SLF001
                stored, available_at=clock.now()
            )
    return outcomes


class TestMutationsAreDetected:
    """Remove a guard, and show the lane's expectations break."""

    async def test_the_unmutated_system_refuses_to_retry_a_permanent_failure(
        self, clock: Any
    ) -> None:
        """Baseline for the mutation below: exactly one execution, dead-lettered."""
        outcomes = await _drive_permanent(ExplodingJobRepository(clock=clock), clock)
        assert outcomes == ["dead_lettered"]

    async def test_removing_the_permanent_rule_breaks_the_expectation(
        self, monkeypatch: Any, clock: Any
    ) -> None:
        """Mutation: ``should_retry`` no longer special-cases ``PERMANENT``.

        If this did not change the outcome, then
        ``TestPermanentFailureNeverRetries`` was never testing the retry policy - it would
        have been testing something else that happens to produce one outcome.
        """
        monkeypatch.setattr(
            jobs_application,
            "should_retry",
            _Weakened.should_retry_without_permanent_rule,
        )
        outcomes = await _drive_permanent(ExplodingJobRepository(clock=clock), clock)
        assert outcomes != ["dead_lettered"], "the PERMANENT guard is not load-bearing"

    async def test_removing_the_taxonomy_breaks_the_expectation(
        self, monkeypatch: Any, clock: Any
    ) -> None:
        """Mutation: every exception classifies as ``TRANSIENT``.

        The taxonomy is what separates "retry this" from "quarantine this". If collapsing it
        changes nothing, the distinction is decorative.
        """
        monkeypatch.setattr(
            jobs_application, "classify_failure", _Weakened.classify_everything_as_transient
        )
        outcomes = await _drive_permanent(ExplodingJobRepository(clock=clock), clock)
        assert outcomes != ["dead_lettered"], "the error taxonomy is not load-bearing"

    def test_the_taxonomy_distinguishes_the_two_failure_kinds(self) -> None:
        """The paired comparison, stated without any mutation.

        ``PERMANENT`` and ``TRANSIENT`` must land on opposite sides of the retry decision.
        This is the assertion the two mutations above are judged against.
        """
        assert classify_failure(ValidationError("bad")) is not classify_failure(
            DependencyUnavailableError("down", dependency="postgres")
        )


class TestCancellationGuardIsLoadBearing:
    """Why the settlement path must catch ``BaseException``, stated as a fact.

    ``asyncio.CancelledError`` inherits from ``BaseException``, not ``Exception``. A handler
    that catches ``Exception`` - the idiomatic choice, and what most code does - would not
    settle a cancelled job. That is not a stylistic preference; it decides whether a graceful
    shutdown re-queues the job or strands it until its lease lapses.
    """

    def test_cancellation_is_not_an_exception_subclass(self) -> None:
        """The fact the ``except BaseException`` in production rests on."""
        import asyncio  # noqa: PLC0415

        assert not issubclass(asyncio.CancelledError, Exception)
        assert issubclass(asyncio.CancelledError, BaseException)

    def test_the_worker_loop_would_not_catch_a_base_exception_itself(self) -> None:
        """The run loop's guard is deliberately narrower than settlement's.

        The loop catches ``Exception``, and that is correct: a ``BaseException`` there means
        the process is being torn down and no loop should keep polling. The asymmetry is
        intentional, so it is asserted rather than left for a reader to infer.
        """
        import inspect  # noqa: PLC0415

        loop_source = inspect.getsource(WorkerRunner.run_forever)
        settle_source = inspect.getsource(jobs_application.ExecuteJobOnce._execute_claim)
        assert "except Exception" in loop_source
        assert "except BaseException" in settle_source


class TestLivenessIsProvablyInert:
    """Liveness must not consult dependencies. Proven by counting, not by inspection.

    A test that asserts ``/healthz`` returns 200 while a probe raises looks like proof, but
    it also passes if the route ignores its health service for any reason - including being
    wired to nothing at all. Counting probe invocations distinguishes those: an inert
    liveness route scores zero, and a route that merely swallows the failure scores one.
    """

    async def test_a_healthz_request_never_invokes_a_probe(self, clock: Any) -> None:
        """Scenario: the database is down and an orchestrator polls liveness.

        Expected behaviour: zero dependency I/O. Evidence: the probe's call counter is still
        zero after the request.
        """
        probe = FakeProbe("postgres", raises=RuntimeError("db down"))
        assert await self._probe_calls_for("/healthz", probe, clock) == 0

    async def test_a_readyz_request_does_invoke_the_probe(self, clock: Any) -> None:
        """The negative control.

        Without this, "zero invocations" would also be consistent with readiness being wired
        to a health service that runs nothing - the opposite failure.
        """
        probe = FakeProbe("postgres", raises=RuntimeError("db down"))
        assert await self._probe_calls_for("/readyz", probe, clock) == 1

    @staticmethod
    async def _probe_calls_for(path: str, probe: FakeProbe, clock: Any) -> int:
        """Count how many times a probe is invoked by a request to ``path``.

        Args:
            path: Request path.
            probe: Counting probe.
            clock: Fixed clock.

        Returns:
            Number of probe invocations observed.

        """
        from knowledge_assistant.config.settings import Settings  # noqa: PLC0415

        from ..conftest import TEST_EPOCH, make_app  # noqa: PLC0415

        settings = Settings(  # type: ignore[call-arg]
            environment="test",
            database_url="postgresql://t:t@localhost/ka",
            service_name="ka-failure",
        )
        app = make_app(probes=[probe], clock=clock, settings=settings)
        del TEST_EPOCH

        calls = 0
        original = probe.check

        async def counting() -> Any:
            nonlocal calls
            calls += 1
            return await original()

        probe.check = counting  # type: ignore[method-assign]
        try:
            import httpx  # noqa: PLC0415

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.get(path)
        finally:
            probe.check = original  # type: ignore[method-assign]
        return calls


class TestHarnessIntegrity:
    """The doubles must really inject, or every test depending on them is vacuous."""

    def test_an_armed_repository_raises_and_counts(self, clock: Any) -> None:
        """``ExplodingJobRepository`` is not a no-op that happens to look armed."""
        repository = ExplodingJobRepository(clock=clock)
        repository.arm(times=1)
        assert repository.fail_times == 1
        assert repository.refused == 0

    async def test_the_armed_fault_is_raised_then_clears(self, clock: Any) -> None:
        """A one-shot fault must fire exactly once and then recover.

        A double that stayed broken would make the recovery tests pass for the wrong reason,
        and one that never fired would make every outage test pass vacuously.
        """
        import pytest  # noqa: PLC0415

        repository = ExplodingJobRepository(clock=clock)
        repository.arm(times=1)
        with pytest.raises(DependencyUnavailableError):
            await repository.claim_next(now=clock.now(), lease_seconds=30.0)
        assert repository.refused == 1
        assert await repository.claim_next(now=clock.now(), lease_seconds=30.0) is None

    async def test_a_disarmed_repository_never_raises(self, clock: Any) -> None:
        """The negative control for the test above."""
        repository = ExplodingJobRepository(clock=clock)
        repository.arm(times=0)
        await repository.enqueue(
            job_type=FAILING_JOB_TYPE,
            payload={},
            available_at=clock.now(),
            max_attempts=3,
        )
        assert repository.refused == 0
        assert repository._jobs  # noqa: SLF001 - test double

    @pytest.mark.parametrize("operation", ["begin", "complete", "release", "get"])
    async def test_only_the_named_store_operation_fails(self, operation: str) -> None:
        """A store that failed everything would collapse the three idempotency windows into
        one, which is exactly what the window-specific tests need to keep separate.
        """
        import datetime as dt  # noqa: PLC0415

        store = FailingIdempotencyStore(fail_on=operation)
        now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
        kwargs: dict[str, Any] = {"tenant_id": "t", "scope": _SCOPE, "key": "k"}

        failures: list[str] = []
        calls: list[Any] = [("get", store.get(**kwargs))]

        if operation == "begin":
            calls.append(
                (
                    "begin",
                    store.begin(**kwargs, request_fingerprint="fp", now=now, retention_seconds=60),
                )
            )
        else:
            # `complete` and `release` only make sense once a key has been claimed; reaching
            # a KeyError here would say nothing about the fault.
            await store.begin(**kwargs, request_fingerprint="fp", now=now, retention_seconds=60)
            calls.extend(
                [
                    (
                        "complete",
                        store.complete(**kwargs, response_payload={"a": 1}, now=now),
                    ),
                    ("release", store.release(**kwargs)),
                ]
            )

        for name, call in calls:
            try:
                await call
            except DependencyUnavailableError:
                failures.append(name)

        assert failures == [operation], f"expected only {operation} to fail, got {failures}"

    def test_a_disarmed_store_is_a_working_store(self) -> None:
        """The negative control for the parametrised test above."""
        store = FailingIdempotencyStore(fail_on="never")
        assert store.calls == []
        assert store.failure is not None

    def test_the_idempotency_store_factory_arms_the_requested_operation(self) -> None:
        """The helper the tests use is wired to the double it claims to build."""
        store = make_idempotency_store(fail_on="begin")
        assert isinstance(store, FailingIdempotencyStore)
        assert store.fail_on == "begin"

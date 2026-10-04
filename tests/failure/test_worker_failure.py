"""Failure injection: worker execution, retry, backoff and dead-lettering.

Covers Phase 0 **OPS-005** ("exponential backoff with jitter and a dead-letter path for
exhausted retries ... a permanently failing job lands in the DLQ with full context after
N attempts") and invariant **DI-11** ("every job's ``attempts <= max_attempts``").

Everything here is deterministic. Backoff is *computed*, never slept: executors are built
with ``jitter_ratio=0.0`` and the injected :class:`FixedClock` does not advance on its own.
A suite that waited for a real 300-second backoff would take five minutes and would be
testing the operating system's scheduler rather than the retry policy.

Executors are wired with :func:`tests.failure.conftest.build_executor` rather than the
shared ``make_executor`` fixture. That fixture closes over the ``job_repository`` fixture,
so an executor built through it is always attached to *that* repository - which would
silently prove nothing in a test about repository failure. Taking the repository as an
argument removes the possibility.

**One defect was found while building this suite and is deliberately not cemented here.**
See :class:`TestAttemptBudgetDiscrepancy`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from knowledge_assistant.application.jobs import JobExecutorOutcome, SubmitJob
from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    InternalError,
    RateLimitedError,
    TimeoutError_,
    ValidationError,
)
from knowledge_assistant.domain.jobs import FailureClass, JobState, classify_failure, should_retry
from knowledge_assistant.workers.handlers import (
    ECHO_JOB_TYPE,
    FAILING_JOB_TYPE,
    FLAKY_JOB_TYPE,
    build_registry,
    failing_handler,
)
from knowledge_assistant.workers.runner import WorkerRunner

from ..conftest import InMemoryJobRepository
from ..failure.conftest import ExplodingJobRepository, build_executor

pytestmark = pytest.mark.failure_injection

#: Backoff parameters every executor in this module is built with, unless a test needs a
#: different cap. Small, readable, and never waited on.
_BASE_SECONDS = 1.0
_CAP_SECONDS = 60.0


def _stored(repository: InMemoryJobRepository) -> Any:
    """Return the single job held by a repository double.

    Args:
        repository: Repository holding exactly one job.

    Returns:
        That job.

    """
    return next(iter(repository._jobs.values()))  # noqa: SLF001 - test double


def _delays(outcomes: list[JobExecutorOutcome], *, now: Any) -> list[float]:
    """Return the backoff delay requested after each retry outcome, in seconds.

    Args:
        outcomes: Outcomes produced by :func:`_drive`.
        now: The fixed clock, whose ``now()`` never advances.

    Returns:
        Delay in seconds for each retry outcome, in order.

    """
    return [
        (o.next_available_at - now.now()).total_seconds()
        for o in outcomes
        if o.outcome == "retry_scheduled"
    ]


def _make_claimable_now(repository: InMemoryJobRepository, job_id: str, *, now: Any) -> None:
    """Pull a retrying job's availability forward to ``now``.

    The *schedule* is asserted through ``outcome.next_available_at``; without this the retry
    is correctly still in the future, the fixed clock never advances, and the driver would
    observe nothing. This is a driver convenience, not a second opinion about backoff.

    ``Job`` is a frozen dataclass, so the row is replaced rather than mutated - mutating it
    would raise ``FrozenInstanceError`` and hide the behaviour under test.

    Args:
        repository: Repository holding the job.
        job_id: Identifier of the job to re-arm.
        now: Instant to make it available at.

    """
    stored = repository._jobs[job_id]  # noqa: SLF001 - test double
    repository._jobs[job_id] = dataclasses.replace(stored, available_at=now)  # noqa: SLF001


async def _drive(
    repository: InMemoryJobRepository,
    clock: Any,
    job_type: str,
    payload: dict[str, object],
    *,
    max_attempts: int = 3,
    backoff_base_seconds: float = _BASE_SECONDS,
    backoff_cap_seconds: float = _CAP_SECONDS,
) -> list[JobExecutorOutcome]:
    """Submit one job and execute until the executor finds nothing claimable.

    The loop runs ``max_attempts + 2`` iterations, not ``max_attempts``. An executor that
    simply stopped early would satisfy a loop bounded by the ceiling, so the slack is what
    makes "the queue drained" evidence rather than an assumption.

    Args:
        repository: Job repository double.
        clock: Fixed clock shared with the repository and the executor.
        job_type: Job type to submit.
        payload: Job payload; for ``system.failing`` this selects the failure mode.
        max_attempts: Attempt ceiling for the job.
        backoff_base_seconds: First retry delay.
        backoff_cap_seconds: Retry delay ceiling.

    Returns:
        Every outcome produced, in order.

    """
    executor = build_executor(
        repository,
        build_registry(),
        clock=clock,
        backoff_base_seconds=backoff_base_seconds,
        backoff_cap_seconds=backoff_cap_seconds,
    )
    job = await SubmitJob(repository, clock=clock).execute(
        job_type=job_type, payload=payload, max_attempts=max_attempts
    )
    outcomes: list[JobExecutorOutcome] = []
    for _ in range(max_attempts + 2):
        outcome = await executor.execute_once()
        if outcome is None:
            break
        outcomes.append(outcome)
        if repository._jobs[job.id].state is JobState.RETRY:  # noqa: SLF001 - test double
            _make_claimable_now(repository, job.id, now=clock.now())
    return outcomes


class TestPermanentFailureNeverRetries:
    """A failure that cannot succeed must not consume the retry budget."""

    async def test_permanent_failure_dead_letters_on_the_first_attempt(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Scenario: the handler raises ``ValidationError``.

        Expected classification: ``PERMANENT``. Retry is **forbidden**: a malformed job is
        equally malformed on attempt two and on attempt fifty, so retrying only delays the
        operator finding out. Evidence: exactly one execution, and it is ``dead_lettered``.

        Retry is idempotent-safe here because there is no retry; that is the point.
        """
        outcomes = await _drive(
            job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "permanent"}
        )
        assert [o.outcome for o in outcomes] == ["dead_lettered"]
        assert outcomes[0].failure_class is FailureClass.PERMANENT

    async def test_permanent_failure_consumes_exactly_one_attempt(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """DI-11: ``attempts_used`` reflects work actually performed, not work offered."""
        await _drive(job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "permanent"})
        stored = _stored(job_repository)
        assert stored.state is JobState.DEAD_LETTERED
        assert stored.attempts_used == 1

    async def test_permanent_failure_is_not_rescheduled(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """A dead-lettered job must not become claimable again.

        Asserted by re-running the executor after the driver loop: an implementation that
        dead-lettered *and* rescheduled would look identical to a correct one until someone
        waited out the backoff.
        """
        await _drive(job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "permanent"})
        executor = build_executor(job_repository, build_registry(), clock=clock)
        assert await executor.execute_once() is None, "a dead-lettered job was reclaimed"

    async def test_dead_lettered_job_retains_its_error_context(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """OPS-005 requires the DLQ entry to carry context, or the queue is a black hole."""
        await _drive(job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "permanent"})
        last_error = _stored(job_repository).last_error
        assert last_error, "a dead-lettered job must record why it died"
        assert "ValidationError" in last_error

    async def test_a_healthy_job_does_not_dead_letter(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The negative control.

        Without this, an executor that dead-lettered *everything* would satisfy every
        failure test in this class while being completely broken. A failure test is only
        evidence of selectivity when the same driver, on the same handler registry, produces
        the opposite outcome for a healthy job.
        """
        outcomes = await _drive(job_repository, clock, ECHO_JOB_TYPE, {"n": 1})
        assert [o.outcome for o in outcomes] == ["succeeded"]
        assert _stored(job_repository).state is JobState.SUCCEEDED


class TestTransientFailureRetriesThenDeadLetters:
    """A recoverable failure is retried on a backoff, then quarantined if it never recovers."""

    async def test_transient_failure_is_retried_before_being_quarantined(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Scenario: the dependency stays unavailable for the whole retry budget.

        Expected classification: ``TRANSIENT``. Retry is **expected**. Evidence: the first
        outcome is a reschedule, not a dead-letter - the exact inversion of
        :class:`TestPermanentFailureNeverRetries`, which is what proves the two failure
        classes are handled differently rather than uniformly.
        """
        outcomes = await _drive(
            job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "transient"}
        )
        assert outcomes[0].outcome == "retry_scheduled"
        assert outcomes[0].failure_class is FailureClass.TRANSIENT

    async def test_transient_failure_ends_in_the_dead_letter_queue(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """A failure that never recovers must be quarantined, not left retrying forever.

        This is the anti-spin requirement behind OPS-005 and DI-11: whatever the attempt
        arithmetic, the terminal state is always the DLQ.
        """
        await _drive(job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "transient"})
        stored = _stored(job_repository)
        assert stored.state is JobState.DEAD_LETTERED
        assert stored.last_error

    @pytest.mark.parametrize("ceiling", [1, 2, 3, 5])
    async def test_attempts_never_exceed_max_attempts(
        self, job_repository: InMemoryJobRepository, clock: Any, ceiling: int
    ) -> None:
        """DI-11, the invariant itself, for every ceiling.

        :func:`_drive` deliberately runs ``max_attempts + 2`` iterations and the assertion is
        written against the *stored row*, not against the number of outcomes the driver saw.
        An executor that never stopped would still pass a test that only counted three.
        """
        await _drive(
            job_repository,
            clock,
            FAILING_JOB_TYPE,
            {"failure_kind": "transient"},
            max_attempts=ceiling,
        )
        stored = _stored(job_repository)
        assert stored.attempts_used <= stored.max_attempts
        assert stored.is_terminal, "a job that never recovers must not stay claimable"

    async def test_backoff_grows_exponentially(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """OPS-005's *exponential* half.

        Asserted as a ratio rather than as absolute seconds: each retry waits twice as long
        as the last. Pinning absolute values here would encode the off-by-one described in
        :class:`TestAttemptBudgetDiscrepancy` into a passing test.
        """
        outcomes = await _drive(
            job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "transient"}, max_attempts=5
        )
        delays = _delays(outcomes, now=clock)
        assert len(delays) >= 2, "not enough retries to observe a schedule"
        assert all(later == 2 * earlier for earlier, later in zip(delays, delays[1:], strict=False))

    async def test_backoff_is_capped(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Pure doubling would reach 8s over four retries; the cap holds it at 2s.

        This is the difference between a backoff that spaces retries and one that parks the
        queue for longer than an operator will tolerate.
        """
        outcomes = await _drive(
            job_repository,
            clock,
            FAILING_JOB_TYPE,
            {"failure_kind": "transient"},
            max_attempts=5,
            backoff_cap_seconds=2.0,
        )
        delays = _delays(outcomes, now=clock)
        assert len(delays) >= 2
        assert max(delays) <= 2.0
        assert max(delays) < _BASE_SECONDS * 2 ** len(delays), "the cap never bound anything"

    async def test_eventually_succeeding_job_is_not_dead_lettered(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Retry must actually *recover*, not merely survive.

        The negative control for this class: if every transient failure dead-lettered, both
        this test and ``test_transient_failure_ends_in_the_dead_letter_queue`` could pass
        while the queue silently dropped recoverable work.
        """
        outcomes = await _drive(
            job_repository, clock, FLAKY_JOB_TYPE, {"fail_until_attempt": 3}, max_attempts=6
        )
        assert outcomes[-1].outcome == "succeeded"
        assert _stored(job_repository).state is JobState.SUCCEEDED

    async def test_a_recovered_job_is_not_redelivered(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """No duplicate side effects: a succeeded job is never claimed again."""
        await _drive(
            job_repository, clock, FLAKY_JOB_TYPE, {"fail_until_attempt": 2}, max_attempts=6
        )
        executor = build_executor(job_repository, build_registry(), clock=clock)
        assert await executor.execute_once() is None


class TestUnknownFailureIsTreatedAsRecoverable:
    """An unclassified failure retries a bounded number of times, then is quarantined."""

    async def test_unknown_failure_is_retried_then_dead_lettered(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Scenario: the handler raises ``InternalError``, which names no retry policy.

        Expected classification: ``UNKNOWN``. Treating unknown as permanent would drop work
        after a single blip; treating it as transient without a ceiling would spin forever.
        Evidence: the same retry-then-quarantine shape as a transient failure.
        """
        outcomes = await _drive(
            job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "unknown"}
        )
        assert outcomes[0].outcome == "retry_scheduled"
        assert outcomes[-1].outcome == "dead_lettered"
        assert outcomes[0].failure_class is FailureClass.UNKNOWN

    async def test_unknown_failure_still_respects_the_ceiling(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """DI-11 applies to unknown failures too, which is the whole point of a ceiling."""
        await _drive(
            job_repository, clock, FAILING_JOB_TYPE, {"failure_kind": "unknown"}, max_attempts=3
        )
        stored = _stored(job_repository)
        assert stored.attempts_used <= stored.max_attempts
        assert stored.state is JobState.DEAD_LETTERED


class TestUnknownJobTypeIsPermanent:
    """A missing handler is a deployment defect, not something a retry can fix."""

    async def test_unknown_job_type_dead_letters_immediately(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Retrying cannot make a handler appear. Retrying would hide a bad deploy behind a
        queue of doomed jobs, so the job is quarantined on attempt one with full context.
        """
        outcomes = await _drive(
            job_repository, clock, "system.registered-in-a-future-deploy", {"n": 1}
        )
        assert [o.outcome for o in outcomes] == ["dead_lettered"]
        assert outcomes[0].failure_class is FailureClass.PERMANENT

    async def test_unknown_job_type_names_the_offending_type(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The operator needs to know *which* job type is unregistered."""
        await _drive(job_repository, clock, "system.registered-in-a-future-deploy", {"n": 1})
        last_error = _stored(job_repository).last_error or ""
        assert "system.registered-in-a-future-deploy" in last_error


class TestWorkerSurvivesRepositoryFailure:
    """The run loop must outlive a repository outage (FR-011, OPS-005)."""

    async def test_worker_keeps_running_while_the_repository_refuses(self, clock: Any) -> None:
        """Scenario: the database is unreachable, so ``claim_next`` raises.

        Injection: :class:`ExplodingJobRepository` armed for two calls. The exception escapes
        ``ExecuteJobOnce.execute_once`` - by design, because no claim was taken and there is
        nothing to settle - and must be absorbed by the run loop.

        A zero poll interval is not a performance trick: the loop's behaviour is identical at
        any interval, and a non-zero one would make this test sleep.
        """
        repository = ExplodingJobRepository(clock=clock)
        await SubmitJob(repository, clock=clock).execute(job_type=ECHO_JOB_TYPE, payload={"n": 1})
        executor = build_executor(repository, build_registry(), clock=clock)
        runner = WorkerRunner(executor, clock=clock, poll_interval_seconds=0.0)

        repository.arm(times=2)
        assert await runner.run_forever(max_iterations=2) == 0
        assert repository.refused == 2, "the fault was never actually reached"

    async def test_a_failed_claim_settles_nothing(self, clock: Any) -> None:
        """State stays consistent: without a claim there is no attempt and no outcome.

        Asserting the job is still ``PENDING`` with ``attempts_used == 0`` is what proves the
        outage did not silently consume the retry budget.
        """
        repository = ExplodingJobRepository(clock=clock)
        await SubmitJob(repository, clock=clock).execute(job_type=ECHO_JOB_TYPE, payload={"n": 1})
        executor = build_executor(repository, build_registry(), clock=clock)
        repository.arm(times=1)
        with pytest.raises(DependencyUnavailableError):
            await executor.execute_once()
        stored = _stored(repository)
        assert stored.state is JobState.PENDING
        assert stored.attempts_used == 0

    async def test_worker_recovers_and_drains_the_queue_when_the_dependency_returns(
        self, clock: Any
    ) -> None:
        """The recovery half.

        Without it, "survived the outage" could equally mean "survived by forgetting the
        job" - a worker that keeps polling an empty queue is also a worker that never died.
        """
        repository = ExplodingJobRepository(clock=clock)
        job = await SubmitJob(repository, clock=clock).execute(
            job_type=ECHO_JOB_TYPE, payload={"n": 1}
        )
        executor = build_executor(repository, build_registry(), clock=clock)
        runner = WorkerRunner(executor, clock=clock, poll_interval_seconds=0.0)

        repository.arm(times=2)
        assert await runner.run_forever(max_iterations=2) == 0

        repository.arm(times=0)  # explicit: the dependency is healthy again
        assert await runner.run_forever(max_iterations=1) == 1
        assert repository.refused == 2
        recovered = await repository.get(job.id)
        assert recovered is not None
        assert recovered.state is JobState.SUCCEEDED

    async def test_a_queue_with_nothing_to_run_executes_nothing(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Idempotency of the empty-queue path.

        This is the negative control for the recovery test above: a loop that reported
        "executed 1" without a claim would satisfy that test while inventing work.
        """
        executor = build_executor(job_repository, build_registry(), clock=clock)
        runner = WorkerRunner(executor, clock=clock, poll_interval_seconds=0.0)
        assert await runner.run_forever(max_iterations=3) == 0
        assert job_repository.settle_calls == []


class TestCancellationIsNotAFailure:
    """A worker stopping mid-job must not burn the job's retry budget."""

    async def test_cancellation_classifies_as_unknown_not_permanent(self) -> None:
        """``asyncio.CancelledError`` is not in the taxonomy, so it is ``UNKNOWN``.

        Dead-lettering on shutdown would quarantine every job the fleet happened to be
        holding when it was asked to stop - turning a rolling restart into data loss.
        """
        assert classify_failure(asyncio.CancelledError()) is FailureClass.UNKNOWN
        assert should_retry(failure_class=FailureClass.UNKNOWN, attempts_used=1, max_attempts=3)

    async def test_cancellation_is_settled_as_a_retry_not_raised(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The settlement path catches ``BaseException``, so cancellation is rescheduled.

        If it only caught ``Exception``, this would propagate and leave the job stuck in
        ``running`` until its lease lapsed - delaying the retry by the full lease duration
        on every ordinary rolling restart.
        """

        async def cancelling(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
            del payload, ctx
            raise asyncio.CancelledError

        executor = build_executor(job_repository, {"system.cancelling": cancelling}, clock=clock)
        await SubmitJob(job_repository, clock=clock).execute(
            job_type="system.cancelling", payload={}
        )
        outcome = await executor.execute_once()
        assert outcome is not None
        assert outcome.outcome == "retry_scheduled"
        assert _stored(job_repository).state is JobState.RETRY


class _NullContext:
    """Job context stand-in for calling a handler directly, without a claimed job."""

    __slots__ = ("attempt", "job_id", "job_type")

    def __init__(self, attempt: int = 1) -> None:
        """Build a context.

        Args:
            attempt: Attempt number.

        """
        self.attempt = attempt
        self.job_id = "job-under-test"
        self.job_type = FAILING_JOB_TYPE


class TestInjectionSeamIntegrity:
    """``system.failing`` is the fault-injection seam. It must inject what it claims.

    Every retry test above classifies by exception type, not by payload. If the handler
    quietly started raising ``InternalError`` for both modes, all of them would still pass -
    as ``UNKNOWN`` - and the distinction between transient and permanent would have stopped
    being tested while the suite stayed green. These tests keep that seam honest.
    """

    @pytest.mark.parametrize(
        ("failure_kind", "expected_class"),
        [
            ("transient", DependencyUnavailableError),
            ("permanent", ValidationError),
            ("unknown", InternalError),
            ("a-mode-nobody-defined", InternalError),
        ],
    )
    async def test_handler_raises_the_taxonomy_class_its_mode_names(
        self, failure_kind: str, expected_class: type[BaseException]
    ) -> None:
        """Scenario: the injection surface is asked for a specific failure mode."""
        with pytest.raises(expected_class) as excinfo:
            await failing_handler({"failure_kind": failure_kind}, _NullContext())
        assert classify_failure(excinfo.value) in set(FailureClass)

    async def test_an_unrecognised_failure_mode_stays_classifiable(self) -> None:
        """Falling out of the taxonomy would make the retry policy a default rather than a
        decision, which is exactly what the explicit mapping exists to prevent.
        """
        with pytest.raises(InternalError) as excinfo:
            await failing_handler({"failure_kind": "not-a-mode"}, _NullContext())
        assert classify_failure(excinfo.value) is FailureClass.UNKNOWN

    async def test_a_missing_failure_mode_defaults_to_unknown(self) -> None:
        """Omitting the key is the most likely mistake a test author can make.

        It must degrade to ``UNKNOWN`` (retried, then quarantined) rather than ``PERMANENT``
        (dropped at once), so a mistyped payload cannot silently turn a transient fault into
        data loss.
        """
        with pytest.raises(InternalError) as excinfo:
            await failing_handler({}, _NullContext())
        assert classify_failure(excinfo.value) is FailureClass.UNKNOWN

    def test_each_taxonomy_class_classifies_as_documented(self) -> None:
        """One mis-classification here would invert every test in this module."""
        assert classify_failure(ValidationError("bad")) is FailureClass.PERMANENT
        assert (
            classify_failure(DependencyUnavailableError("down", dependency="postgres"))
            is FailureClass.TRANSIENT
        )
        assert (
            classify_failure(TimeoutError_("slow", dependency="postgres", timeout_seconds=2.0))
            is FailureClass.TRANSIENT
        )
        assert (
            classify_failure(RateLimitedError("later", retry_after_seconds=5, scope="tenant"))
            is FailureClass.TRANSIENT
        )
        assert classify_failure(InternalError("?")) is FailureClass.UNKNOWN
        assert classify_failure(RuntimeError("outside the taxonomy")) is FailureClass.UNKNOWN


class TestAttemptBudgetDiscrepancy:
    """**Known defect, recorded rather than cemented.** OPS-005 is not fully met.

    Both repository adapters - the in-memory double used here and the SQL adapter in
    ``infrastructure/db/jobs.py`` - increment ``attempts_used`` in ``claim_next``. But
    ``ExecuteJobOnce._settle_failure`` then computes ``attempts_used = job.attempts_used + 1``
    *again*, so the retry decision compares ``attempts + 1`` against ``max_attempts``. The
    observable consequences, measured by :func:`_drive`:

    ======================================  ==================  ====================
    ``max_attempts``                       executions observed  ``attempts_used`` row
    ======================================  ==================  ====================
    1                                      1                   1
    2                                      1                   1
    3                                      2                   2
    5                                      4                   4
    ======================================  ==================  ====================

    A transient failure therefore gets ``max_attempts - 1`` attempts, not ``max_attempts``,
    and the first backoff delay is ``base x 2`` rather than ``base``. Phase 0's state machine
    (``docs/phase0/08-recommended-architecture.md``, "attempts < max?") and its retry table
    ("Max attempts 5" for retryable classes, "1" for deterministic ones) both describe the
    latter. With ``max_attempts=2`` a transient failure gets **zero** retries.

    DI-11 still holds - ``attempts_used`` never exceeds ``max_attempts`` - so the invariant
    suite above is green and would not have caught this on its own.

    These tests assert the *documented* behaviour and are marked ``xfail(strict=True)``.
    Strict mode means fixing the off-by-one turns them into failures, which forces the
    marker to be removed in the same change that fixes it - so the fix cannot be made and
    then forgotten about.
    """

    @pytest.mark.parametrize("ceiling", [2, 3, 5])
    @pytest.mark.xfail(
        strict=True,
        reason="OPS-005: claim_next already counted the attempt, so the budget is short by one",
    )
    async def test_a_transient_failure_gets_the_configured_number_of_attempts(
        self, job_repository: InMemoryJobRepository, clock: Any, ceiling: int
    ) -> None:
        """A job configured for ``max_attempts`` attempts should execute that many times."""
        outcomes = await _drive(
            job_repository,
            clock,
            FAILING_JOB_TYPE,
            {"failure_kind": "transient"},
            max_attempts=ceiling,
        )
        assert len(outcomes) == ceiling

    @pytest.mark.xfail(
        strict=True,
        reason="same off-by-one: the first retry is scheduled with attempt=2, not attempt=1",
    )
    async def test_the_first_backoff_delay_is_the_configured_base(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Phase 0's schedule is ``min(cap, base x 2^n)`` with ``n=0`` on the first failure."""
        outcomes = await _drive(
            job_repository,
            clock,
            FAILING_JOB_TYPE,
            {"failure_kind": "transient"},
            backoff_base_seconds=_BASE_SECONDS,
        )
        assert _delays(outcomes, now=clock)[0] == _BASE_SECONDS

    async def test_the_shortfall_is_one_attempt_and_not_open_ended(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """A passing characterisation test.

        It records the current arithmetic without blessing it: the gap between configured and
        observed attempts is exactly one, and at least one attempt always happens. If a future
        change made the shortfall unbounded, this test fails even though the xfails above
        might be quietly deleted.
        """
        for ceiling in (1, 2, 3, 5):
            observed = await _drive(
                job_repository,
                clock,
                FAILING_JOB_TYPE,
                {"failure_kind": "transient"},
                max_attempts=ceiling,
            )
            assert len(observed) == max(1, ceiling - 1)

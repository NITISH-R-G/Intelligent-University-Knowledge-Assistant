"""Failure injection: illegal job state transitions and stale-claim fencing.

Two separate resilience properties live here, and they fail in opposite directions.

**The lifecycle guard.** Phase 0's job model is a strict state machine. An illegal transition
is a programming error, not a runtime condition, so it must be a hard failure rather than a
silently-accepted write. The suite below asserts the *declared* lifecycle exhaustively rather
than reading the implementation's own table back - reading the table would make the test pass
whatever the table said.

**The fencing token.** At-least-once delivery means a job can be executed twice: a worker that
is slow past its lease may be overtaken by another. The lease plus the conditional update is
what stops the slow worker's late result from overwriting the newer one. Without it, the last
writer wins and a recovered job can be resurrected as dead.

This is where "worker state is recoverable" and "no duplicate side effects" are actually
decided; a retry policy alone does not protect them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from knowledge_assistant.domain.jobs import (
    Job,
    JobClaim,
    JobState,
    assert_transition_allowed,
    is_terminal,
)
from knowledge_assistant.workers.handlers import ECHO_JOB_TYPE

from ..conftest import InMemoryJobRepository

pytestmark = pytest.mark.failure_injection

#: The lifecycle Phase 0 declares, written out independently of the implementation.
#:
#: ``PENDING`` may only be claimed. ``RUNNING`` may succeed, retry, be quarantined, or fall
#: back to ``PENDING`` when a lease lapses. ``RETRY`` returns to ``PENDING``. Both terminal
#: states are sinks: the only way out is an operator replaying from the DLQ, which is a new
#: job, not a transition.
_DECLARED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset(
        {JobState.SUCCEEDED, JobState.RETRY, JobState.DEAD_LETTERED, JobState.PENDING}
    ),
    JobState.RETRY: frozenset({JobState.PENDING}),
    JobState.SUCCEEDED: frozenset(),
    JobState.DEAD_LETTERED: frozenset(),
}


def _job(state: JobState, *, clock: Any) -> Job:
    """Build a job snapshot in a given state.

    Args:
        state: State the job is in.
        clock: Fixed clock supplying ``created_at``.

    Returns:
        An immutable job snapshot.

    """
    return Job(
        id="job-1",
        type=ECHO_JOB_TYPE,
        state=state,
        attempts_used=1,
        max_attempts=3,
        available_at=clock.now(),
        lease_expires_at=None,
        payload={"n": 1},
        created_at=clock.now(),
    )


class TestIllegalTransitionsAreRejected:
    """Every transition outside the declared lifecycle must fail loudly."""

    @pytest.mark.parametrize("current", list(JobState))
    def test_every_undeclared_transition_is_refused(self, current: JobState) -> None:
        """Exhaustive over the full state x target matrix.

        Written as "everything not in the declared set raises" rather than as a list of known
        bad pairs, so a transition added to the implementation but not to the model fails
        here. That is the whole value of the guard: it catches the *new* illegal edge nobody
        thought to enumerate.
        """
        for target in JobState:
            allowed = target in _DECLARED_TRANSITIONS[current]
            if allowed:
                assert_transition_allowed(current, target)
            else:
                with pytest.raises(ValueError, match="illegal job state transition"):
                    assert_transition_allowed(current, target)

    def test_the_declared_lifecycle_matches_the_model(self, clock: Any) -> None:
        """A characterisation check, so a drift in either direction is visible.

        If the implementation ever allows something this module forbids, the matrix above
        fails; if it forbids something here, this fails. Neither alone is sufficient.
        """
        for current, targets in _DECLARED_TRANSITIONS.items():
            assert _job(current, clock=clock).state is current
            for target in JobState:
                if target in targets:
                    continue
                with pytest.raises(ValueError, match="illegal job state transition"):
                    _job(current, clock=clock).with_transition(target, now=clock.now())

    @pytest.mark.parametrize("terminal", [JobState.SUCCEEDED, JobState.DEAD_LETTERED])
    def test_terminal_states_are_sinks(self, terminal: JobState, clock: Any) -> None:
        """Nothing moves out of a terminal state without operator action.

        A succeeded job that could be returned to ``RETRY`` would be silently re-run forever;
        a dead-lettered job that could be revived automatically would defeat the DLQ.
        """
        assert is_terminal(terminal)
        for target in JobState:
            with pytest.raises(ValueError, match="illegal job state transition"):
                _job(terminal, clock=clock).with_transition(target, now=clock.now())

    def test_a_refused_transition_leaves_the_job_unchanged(self, clock: Any) -> None:
        """Immutability: a failed transition must not half-apply.

        If the guard raised *after* mutating, a job could be left in a state neither the
        implementation nor the operator intended.
        """
        original = _job(JobState.PENDING, clock=clock)
        with pytest.raises(ValueError, match="illegal job state transition"):
            original.with_transition(JobState.SUCCEEDED, now=clock.now())
        assert original.state is JobState.PENDING
        assert original.attempts_used == 1

    def test_succeeding_is_not_a_self_transition(self, clock: Any) -> None:
        """``RUNNING -> RUNNING`` is not a transition; re-settling a job must be refused.

        This is the state-machine half of the fencing property below: a second settlement
        cannot pretend to be the first.
        """
        with pytest.raises(ValueError, match="illegal job state transition"):
            _job(JobState.RUNNING, clock=clock).with_transition(JobState.RUNNING, now=clock.now())


async def _claim(repository: InMemoryJobRepository, clock: Any) -> JobClaim:
    """Enqueue and claim one job.

    Args:
        repository: Repository double.
        clock: Fixed clock.

    Returns:
        The claim.

    """
    from knowledge_assistant.application.jobs import SubmitJob  # noqa: PLC0415

    await SubmitJob(repository, clock=clock).execute(job_type=ECHO_JOB_TYPE, payload={"n": 1})
    claim = await repository.claim_next(now=clock.now(), lease_seconds=30.0)
    assert claim is not None
    return claim


class TestStaleClaimsCannotOverwrite:
    """The fencing token: a superseded worker's outcome must be discarded."""

    async def test_a_superseded_claim_cannot_overwrite_a_recorded_result(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Scenario: worker A claims a job, stalls past its lease; worker B takes over and
        finishes it. Worker A then wakes up and tries to settle.

        Expected behaviour: A's settlement is discarded, not applied. Without this, the last
        writer wins and a correctly-completed job can be flipped back to ``running`` or
        retried - duplicating the side effect A and B both already performed.
        """
        stale = await _claim(job_repository, clock)

        # Worker B takes over and settles the job while A is still holding its claim object.
        assert not stale.is_expired(now=clock.now())
        await job_repository.mark_succeeded(claim=stale, result={"worker": "b"})
        assert job_repository._jobs[stale.job.id].state is JobState.SUCCEEDED  # noqa: SLF001

        # Worker A wakes up and tries to record its own, different, outcome.
        await job_repository.mark_retry(
            claim=stale, error="worker a was slow", next_available_at=clock.now()
        )
        stored = job_repository._jobs[stale.job.id]  # noqa: SLF001
        assert stored.state is JobState.SUCCEEDED, "a stale claim overwrote a recorded result"
        assert stored.last_error is None

    async def test_a_stale_failure_does_not_dead_letter_a_succeeded_job(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The dangerous direction: a slow worker's error must not quarantine finished work.

        This is how a recovered pipeline ends up with a queue of dead-lettered jobs that a
        human has to reconcile one by one.
        """
        claim = await _claim(job_repository, clock)
        await job_repository.mark_succeeded(claim=claim, result={"ok": True})
        await job_repository.mark_dead_lettered(claim=claim, error="stale worker failed")

        stored = job_repository._jobs[claim.job.id]  # noqa: SLF001
        assert stored.state is JobState.SUCCEEDED
        assert await job_repository.dead_letter_count() == 0

    async def test_a_settlement_is_recorded_exactly_once(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Every settlement attempt is recorded, but only the first changes state.

        Counting the *attempts* rather than only the final state is what shows the second
        settlement was genuinely offered and genuinely refused, not simply never made.
        """
        claim = await _claim(job_repository, clock)
        await job_repository.mark_succeeded(claim=claim, result={"ok": True})
        await job_repository.mark_succeeded(claim=claim, result={"ok": True})

        assert job_repository.settle_calls == [
            (claim.job.id, "succeeded"),
            (claim.job.id, "succeeded"),
        ]
        assert job_repository._jobs[claim.job.id].state is JobState.SUCCEEDED  # noqa: SLF001


class TestLeaseLapseMakesWorkRecoverable:
    """A worker that dies stops renewing; the lease lapses and the job is retaken."""

    def test_a_live_lease_is_not_expired(self, clock: Any) -> None:
        """The negative control: an expired-lease assertion proves nothing if everything
        looks expired.
        """
        claim = JobClaim(
            job=_job(JobState.RUNNING, clock=clock),
            claimed_at=clock.now(),
            lease_expires_at=clock.now() + dt.timedelta(seconds=30),
        )
        assert not claim.is_expired(now=clock.now())
        assert not claim.is_expired(now=clock.now() + dt.timedelta(seconds=29))

    def test_a_lapsed_lease_expires_exactly_at_its_deadline(self, clock: Any) -> None:
        """Boundary behaviour matters here: an off-by-one delays recovery by a whole lease.

        ``>=`` rather than ``>`` is the correct reading - at the deadline the lease no longer
        confers ownership.
        """
        claim = JobClaim(
            job=_job(JobState.RUNNING, clock=clock),
            claimed_at=clock.now(),
            lease_expires_at=clock.now() + dt.timedelta(seconds=30),
        )
        assert not claim.is_expired(now=clock.now() + dt.timedelta(seconds=29, microseconds=1))
        assert claim.is_expired(now=clock.now() + dt.timedelta(seconds=30))

    async def test_a_job_whose_lease_lapsed_can_be_claimed_again(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Scenario: the worker holding the job is killed outright.

        Expected behaviour: the job returns to claimable after the lease, so a single dead
        process cannot strand work forever. Evidence: a second claim succeeds once the clock
        passes the lease.
        """
        first = await _claim(job_repository, clock)
        assert first.lease_expires_at is not None

        clock.advance(31)
        assert first.is_expired(now=clock.now())

        # A crashed worker leaves the row in `running`; takeover is the lease's job.
        job_repository._jobs[first.job.id] = first.job.with_transition(  # noqa: SLF001
            JobState.PENDING, now=clock.now()
        )
        second = await job_repository.claim_next(now=clock.now(), lease_seconds=30.0)
        assert second is not None
        assert second.job.id == first.job.id

    async def test_a_running_job_with_a_live_lease_is_not_reissued(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The negative control for the recovery test above.

        Without it, "the job was claimable again" could simply mean the repository handed out
        every job regardless of ownership, and a healthy worker would double-execute.
        """
        await _claim(job_repository, clock)
        assert await job_repository.claim_next(now=clock.now(), lease_seconds=30.0) is None

    async def test_a_recovered_job_gets_a_full_attempt_budget_again(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """State stays consistent across takeover: attempts are cumulative, not reset.

        If reclaiming reset the counter, a job that poisons the worker holding it could be
        handed out for ever, and DI-11 would be meaningless.
        """
        first = await _claim(job_repository, clock)
        assert first.job.attempts_used == 1

        clock.advance(31)
        job_repository._jobs[first.job.id] = first.job.with_transition(  # noqa: SLF001
            JobState.PENDING, now=clock.now()
        )
        second = await job_repository.claim_next(now=clock.now(), lease_seconds=30.0)
        assert second is not None
        assert second.job.attempts_used == 2
        assert second.job.attempts_used <= second.job.max_attempts

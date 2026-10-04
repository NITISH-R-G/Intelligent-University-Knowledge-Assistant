"""Unit tests for job retry classification, backoff and the state machine.

These are the tests that matter most in Phase 1: retry policy is where self-inflicted outages
live. Each test names the failure it prevents in its docstring, because "test_backoff_works" tells
a reader nothing while "a retry storm after recovery re-creates the outage" tells them everything.
"""

from __future__ import annotations

import datetime as dt

import pytest

from knowledge_assistant.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    InternalError,
    RateLimitedError,
    TimeoutError_,
    ValidationError,
)
from knowledge_assistant.domain.jobs import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS_CEILING,
    FailureClass,
    Job,
    JobState,
    apply_jitter,
    assert_transition_allowed,
    base_delay_seconds,
    classify_failure,
    is_terminal,
    next_attempt_at,
    should_retry,
)

pytestmark = pytest.mark.unit


class TestClassification:
    """Which failures are worth retrying."""

    def test_dependency_failure_is_transient(self) -> None:
        """A database being down must be retried: it will come back."""
        assert classify_failure(DependencyUnavailableError("x", dependency="postgres")) is (
            FailureClass.TRANSIENT
        )

    def test_timeout_is_transient(self) -> None:
        """A timeout is the archetypal transient failure."""
        assert (
            classify_failure(TimeoutError_("x", dependency="postgres", timeout_seconds=1.0))
            is FailureClass.TRANSIENT
        )

    def test_rate_limit_is_transient(self) -> None:
        """A rate limit is transient; retrying after Retry-After is the correct response."""
        assert (
            classify_failure(RateLimitedError("x", retry_after_seconds=1, scope="t"))
            is FailureClass.TRANSIENT
        )

    def test_validation_is_permanent(self) -> None:
        """Retrying malformed input forever is the classic infinite-retry bug."""
        assert classify_failure(ValidationError("bad", detail={"field": "x"})) is (
            FailureClass.PERMANENT
        )

    def test_conflict_is_permanent(self) -> None:
        """A conflict requires a new decision from the caller, not a retry."""
        assert classify_failure(ConflictError("stale")) is FailureClass.PERMANENT

    def test_unknown_exception_is_unknown_not_permanent(self) -> None:
        """An unmapped exception must not be treated as permanent.

        Treating unknown as permanent silently drops work after one transient blip, which is
        how real pipelines lose jobs without any error being raised.
        """
        assert classify_failure(RuntimeError("boom")) is FailureClass.UNKNOWN
        assert classify_failure(InternalError("boom")) is FailureClass.UNKNOWN

    def test_unknown_failures_still_stop_at_the_ceiling(self) -> None:
        """Unknown must retry a bounded number of times, then dead-letter."""
        assert should_retry(failure_class=FailureClass.UNKNOWN, attempts_used=2, max_attempts=3)
        assert not should_retry(failure_class=FailureClass.UNKNOWN, attempts_used=3, max_attempts=3)


class TestBackoff:
    """Delay computation."""

    def test_first_attempt_uses_the_base(self) -> None:
        """Attempt 1 waits exactly the configured base."""
        assert base_delay_seconds(1, base_seconds=2.0, cap_seconds=60.0) == 2.0

    def test_delay_doubles(self) -> None:
        """Each subsequent attempt doubles the wait."""
        delays = [base_delay_seconds(n, base_seconds=1.0, cap_seconds=1000.0) for n in range(1, 6)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_delay_is_capped(self) -> None:
        """Without a cap, a long outage pushes retries past human attention."""
        assert base_delay_seconds(50, base_seconds=1.0, cap_seconds=300.0) == 300.0

    def test_attempt_must_be_positive(self) -> None:
        """Attempt 0 is a programming error, not a silent default."""
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            base_delay_seconds(0)

    def test_non_positive_bounds_rejected(self) -> None:
        """A zero or negative cap would make the delay zero and defeat backoff."""
        with pytest.raises(ValueError, match="must be positive"):
            base_delay_seconds(1, base_seconds=1.0, cap_seconds=0.0)

    def test_large_attempt_does_not_overflow(self) -> None:
        """Attempt numbers are bounded by MAX_ATTEMPTS_CEILING, but the function must not
        overflow computing an intermediate power of two.
        """
        assert base_delay_seconds(10_000, base_seconds=1.0, cap_seconds=300.0) == 300.0


class TestJitter:
    """Jitter prevents synchronised retry storms."""

    def test_jitter_is_bounded_by_ratio(self) -> None:
        """Jitter must never exceed the configured fraction in either direction."""
        for unit in (0.0, 0.25, 0.5, 0.75, 0.999999):
            value = apply_jitter(100.0, unit, ratio=0.2)
            assert 80.0 <= value <= 120.0

    def test_jitter_is_symmetric_around_the_delay(self) -> None:
        """One-sided jitter would bias every retry late, inflating mean latency."""
        low = apply_jitter(100.0, 0.0, ratio=0.2)
        high = apply_jitter(100.0, 0.999999, ratio=0.2)
        assert low == pytest.approx(80.0)
        assert high == pytest.approx(120.0, rel=1e-3)

    def test_unit_interval_is_validated(self) -> None:
        """A jitter source outside [0,1) indicates a broken random source."""
        with pytest.raises(ValueError, match=r"unit_interval must be in \[0, 1\)"):
            apply_jitter(1.0, 1.0)
        with pytest.raises(ValueError, match=r"unit_interval must be in \[0, 1\)"):
            apply_jitter(1.0, -0.1)

    def test_result_is_never_negative(self) -> None:
        """A negative delay would schedule a retry in the past, spinning immediately."""
        assert apply_jitter(1.0, 0.0, ratio=1.0) >= 0.0

    def test_next_attempt_at_is_in_the_future(self) -> None:
        """A retry must be scheduled strictly after now."""
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        when = next_attempt_at(
            now=now, attempt=1, base_seconds=1.0, cap_seconds=60.0, unit_interval=0.0
        )
        assert when > now


class TestStateMachine:
    """Legal transitions."""

    def test_terminal_states_have_no_exits(self) -> None:
        """A succeeded or dead-lettered job must not be claimable again."""
        for state in (JobState.SUCCEEDED, JobState.DEAD_LETTERED):
            assert is_terminal(state)
            with pytest.raises(ValueError, match="illegal job state transition"):
                assert_transition_allowed(state, JobState.PENDING)

    def test_succeeded_cannot_go_back_to_running(self) -> None:
        """The exact bug that a naive UPDATE would allow."""
        with pytest.raises(ValueError):
            assert_transition_allowed(JobState.SUCCEEDED, JobState.RUNNING)

    def test_pending_may_only_be_claimed(self) -> None:
        """A pending job cannot jump straight to success; it must be claimed first."""
        with pytest.raises(ValueError):
            assert_transition_allowed(JobState.PENDING, JobState.SUCCEEDED)

    def test_claim_increments_attempts(self) -> None:
        """Attempt counting must happen on claim, so a crash still consumes an attempt."""
        job = Job(
            id="j1",
            type="t",
            state=JobState.PENDING,
            attempts_used=0,
            max_attempts=3,
            available_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            lease_expires_at=None,
            payload={},
        )
        claimed = job.with_transition(JobState.RUNNING, now=job.available_at)
        assert claimed.attempts_used == 1
        assert claimed.state is JobState.RUNNING


class TestAttemptCeiling:
    """The ceiling is a safety rail, not a policy."""

    def test_ceiling_is_enforced_by_the_domain(self) -> None:
        """MAX_ATTEMPTS_CEILING exists so a misconfigured value cannot loop forever."""
        assert MAX_ATTEMPTS_CEILING == 10
        assert DEFAULT_MAX_ATTEMPTS <= MAX_ATTEMPTS_CEILING

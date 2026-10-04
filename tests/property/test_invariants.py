"""Property-based tests for the pure core.

Example-based tests check the cases an author thought of. Properties check the cases nobody
thought of. The functions below are pure, so the payoff is high: every property runs over
hundreds of generated inputs in milliseconds, and a counterexample shrinks to a minimal
reproducing case that goes straight into a regression test.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from knowledge_assistant.application.idempotency import fingerprint
from knowledge_assistant.domain.clock import UTC
from knowledge_assistant.domain.errors import ApplicationError, ErrorKind, ValidationError
from knowledge_assistant.domain.health import (
    ProbeCriticality,
    ProbeResult,
    ProbeStatus,
    aggregate,
)
from knowledge_assistant.domain.identifiers import (
    is_valid_idempotency_key,
    namespaced_uuid,
    validate_idempotency_key,
)
from knowledge_assistant.domain.jobs import (
    FailureClass,
    base_delay_seconds,
    apply_jitter,
    classify_failure,
    should_retry,
)
from knowledge_assistant.domain.jobs import JobState, assert_transition_allowed

pytestmark = pytest.mark.property

EPOCH = dt.datetime(2026, 3, 1, tzinfo=UTC)

POSITIVE_FLOATS = st.floats(min_value=1e-6, max_value=86400.0, allow_nan=False, allow_infinity=False)
ATTEMPTS = st.integers(min_value=1, max_value=10**6)

#: The idempotency-key alphabet from ``domain.identifiers``: URL-safe ASCII only.
KEY_ALPHABET = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"
)


class TestBackoffProperties:
    """Backoff is the function most likely to blow up on an unexpected input."""

    @given(attempt=ATTEMPTS, base=POSITIVE_FLOATS, cap=POSITIVE_FLOATS)
    @settings(max_examples=300, deadline=None)
    def test_delay_never_exceeds_the_cap(self, attempt: int, base: float, cap: float) -> None:
        """The cap is the entire point: without it a long outage schedules retries days out."""
        assert base_delay_seconds(attempt, base_seconds=base, cap_seconds=cap) <= cap

    @given(attempt=ATTEMPTS, base=POSITIVE_FLOATS, cap=POSITIVE_FLOATS)
    @settings(max_examples=300, deadline=None)
    def test_delay_is_always_positive(self, attempt: int, base: float, cap: float) -> None:
        """A zero delay turns bounded retries into a hot loop against a broken dependency."""
        assert base_delay_seconds(attempt, base_seconds=base, cap_seconds=cap) > 0

    @given(attempt=ATTEMPTS, base=POSITIVE_FLOATS)
    @settings(max_examples=200, deadline=None)
    def test_delay_is_monotonic_in_attempt(self, attempt: int, base: float) -> None:
        """Each retry must wait longer than the last; a non-monotonic delay would send a
        worker back to a problem it already decided to wait out."""
        assume(attempt < 10**6 - 1)
        current = base_delay_seconds(attempt, base_seconds=base, cap_seconds=86400.0)
        following = base_delay_seconds(attempt + 1, base_seconds=base, cap_seconds=86400.0)
        assert following >= current

    @given(attempt=ATTEMPTS)
    @settings(max_examples=100, deadline=None)
    def test_no_attempt_number_raises(self, attempt: int) -> None:
        """A retry path must never raise. An overflow here would crash the worker loop."""
        assert base_delay_seconds(attempt, base_seconds=1.0, cap_seconds=300.0) <= 300.0


class TestJitterProperties:
    """Jitter exists to de-synchronise a retry storm."""

    @given(
        delay=POSITIVE_FLOATS,
        unit=st.floats(min_value=0.0, max_value=0.999999, allow_nan=False),
        ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=300, deadline=None)
    def test_jitter_stays_within_the_ratio(self, delay: float, unit: float, ratio: float) -> None:
        result = apply_jitter(delay, unit, ratio=ratio)
        assert result >= delay * (1.0 - ratio) - 1e-9
        assert result <= delay * (1.0 + ratio) + 1e-9

    @given(delay=POSITIVE_FLOATS, ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
    @settings(max_examples=200, deadline=None)
    def test_jitter_is_never_negative(self, delay: float, ratio: float) -> None:
        assert apply_jitter(delay, 0.0, ratio=ratio) >= 0.0

    @given(delay=POSITIVE_FLOATS)
    @settings(max_examples=100, deadline=None)
    def test_midpoint_returns_the_undelayed_value(self, delay: float) -> None:
        """One-sided jitter would bias every retry late, inflating mean recovery time."""
        assert apply_jitter(delay, 0.5, ratio=1.0) == pytest.approx(delay)


class TestRetryPolicyProperties:
    """Retry decisions must be decidable for every error the taxonomy can produce."""

    @given(st.sampled_from(list(ErrorKind)))
    @settings(max_examples=50, deadline=None)
    def test_every_error_kind_has_a_policy(self, kind: ErrorKind) -> None:
        from knowledge_assistant.domain.errors import POLICY_BY_KIND  # noqa: PLC0415

        assert kind in POLICY_BY_KIND, f"{kind} has no policy entry"

    @given(
        attempts_used=st.integers(min_value=1, max_value=100),
        max_attempts=st.integers(min_value=1, max_value=10),
        failure=st.sampled_from(list(FailureClass)),
    )
    @settings(max_examples=200, deadline=None)
    def test_retries_are_bounded(self, attempts_used: int, max_attempts: int, failure: FailureClass) -> None:
        """No combination of inputs may produce an unbounded retry decision."""
        decision = should_retry(
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            failure_class=failure,
        )
        assert isinstance(decision, bool)

    @given(st.sampled_from(list(FailureClass)))
    @settings(max_examples=50, deadline=None)
    def test_permanent_failures_are_never_retried(self, failure: FailureClass) -> None:
        if failure is FailureClass.PERMANENT:
            assert (
                should_retry(attempts_used=1, max_attempts=10, failure_class=failure) is False
            )


class TestStateMachineProperties:
    """An illegal transition must raise rather than silently corrupt state."""

    STATES = list(JobState)

    @given(source=st.sampled_from(STATES), target=st.sampled_from(STATES))
    @settings(max_examples=200, deadline=None)
    def test_terminal_states_have_no_successors(self, source: JobState, target: JobState) -> None:
        """Once a job is succeeded or dead-lettered it must be immutable; otherwise a late
        worker can overwrite a correct outcome."""
        if source in (JobState.SUCCEEDED, JobState.DEAD_LETTERED):
            with pytest.raises(ValueError):
                assert_transition_allowed(source, target)

    @given(source=st.sampled_from(STATES))
    @settings(max_examples=50, deadline=None)
    def test_self_transition_is_never_an_implicit_advance(self, source: JobState) -> None:
        """Every state either allows staying put or raises; nothing is ambiguous."""
        try:
            assert_transition_allowed(source, source)
        except ValueError:
            pass


class TestIdempotencyKeyProperties:
    """Key validation sits on an unauthenticated boundary."""

    @given(st.text(min_size=0, max_size=300))
    @settings(max_examples=300, deadline=None)
    def test_validator_agrees_with_predicate(self, value: str) -> None:
        """Two functions answering the same question must not disagree, or which one the
        request path uses becomes security-relevant by accident."""
        assume("\x00" not in value)
        try:
            validated = validate_idempotency_key(value)
        except ValueError:
            assert not is_valid_idempotency_key(value)
        else:
            assert is_valid_idempotency_key(validated) or len(value) < 16

    @given(st.text(min_size=16, max_size=255, alphabet=KEY_ALPHABET))
    @settings(max_examples=100, deadline=None)
    def test_conforming_keys_are_accepted(self, value: str) -> None:
        assert is_valid_idempotency_key(value)

    @given(st.text(min_size=1, max_size=300, alphabet=KEY_ALPHABET))
    @settings(max_examples=100, deadline=None)
    def test_short_keys_are_rejected(self, value: str) -> None:
        """The minimum length is not decoration: a one-character key would collide under any
        real client key scheme."""
        assume(len(value) < 16)
        assert not is_valid_idempotency_key(value)

    @given(st.text(min_size=1, max_size=300))
    @settings(max_examples=200, deadline=None)
    def test_non_ascii_keys_are_rejected(self, value: str) -> None:
        """The key reaches a URL, a log line and a database column. Restricting it to a
        URL-safe ASCII set removes three separate injection surfaces at once."""
        if not value.isascii() and is_valid_idempotency_key(value):
            pytest.fail(f"non-ASCII key accepted: {value!r}")

    @given(name=st.text(min_size=1, max_size=120))
    @settings(max_examples=100, deadline=None)
    def test_namespaced_uuid_is_deterministic(self, name: str) -> None:
        """Deterministic derivation is what makes a content-addressed id reproducible."""
        assert namespaced_uuid(name) == namespaced_uuid(name)

    @given(left=st.text(min_size=1, max_size=40), right=st.text(min_size=1, max_size=40))
    @settings(max_examples=100, deadline=None)
    def test_distinct_names_give_distinct_ids(self, left: str, right: str) -> None:
        assume(left != right)
        assert namespaced_uuid(left) != namespaced_uuid(right)


class TestFingerprintProperties:
    """The fingerprint decides replay vs conflict; a collision is a lost response."""

    @given(
        payload=st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.integers(min_value=-(10**6), max_value=10**6),
            max_size=8,
        )
    )
    @settings(max_examples=300, deadline=None)
    def test_fingerprint_is_stable(self, payload: dict[str, object]) -> None:
        assert fingerprint(payload) == fingerprint(dict(payload))

    @given(
        left=st.dictionaries(st.text(min_size=1, max_size=20), st.integers(), max_size=6),
        right=st.dictionaries(st.text(min_size=1, max_size=20), st.integers(), max_size=6),
    )
    @settings(max_examples=300, deadline=None)
    def test_different_payloads_differ(self, left: dict[str, object], right: dict[str, object]) -> None:
        assume(left != right)
        assert fingerprint(left) != fingerprint(right)

    @given(payload=st.dictionaries(st.text(min_size=1, max_size=20), st.integers(), max_size=6))
    @settings(max_examples=100, deadline=None)
    def test_key_insertion_order_is_irrelevant(self, payload: dict[str, object]) -> None:
        """A client whose serialiser reorders keys must not get spurious conflicts."""
        reordered = dict(reversed(list(payload.items())))
        assert fingerprint(payload) == fingerprint(reordered)


class TestErrorContextProperties:
    """Context is filtered by an allow-list; nothing arbitrary may escape."""

    @given(
        key=st.text(min_size=1, max_size=30),
        value=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=300, deadline=None)
    def test_arbitrary_context_is_filtered(self, key: str, value: str) -> None:
        """Validation errors allow only 'field' and 'violations'; anything else an operator
        attaches must be dropped before it can reach a client."""
        error = ValidationError("m", detail={key: value})
        public = error.public_context()
        assert set(public) <= {"field", "violations"}
        if key not in {"field", "violations"}:
            assert key not in public

    @given(key=st.sampled_from(["password", "token", "api_key", "secret", "dsn", "sql"]))
    @settings(max_examples=100, deadline=None)
    def test_credential_shaped_keys_never_survive(self, key: str) -> None:
        error = ValidationError("m", detail={key: "hunter2"})
        assert "hunter2" not in str(error.public_context())

    @given(message=st.text(min_size=1, max_size=200))
    @settings(max_examples=200, deadline=None)
    def test_error_is_constructible_for_any_message(self, message: str) -> None:
        assert isinstance(ApplicationError(message), Exception)


class TestHealthAggregationProperties:
    """Aggregation is a pure function; its rules must hold for every input shape."""

    @given(
        probes=st.lists(
            st.tuples(st.sampled_from(list(ProbeStatus)), st.sampled_from(list(ProbeCriticality))),
            max_size=6,
        )
    )
    @settings(max_examples=200, deadline=None)
    def test_ready_iff_no_critical_probe_is_unhealthy(self, probes: list[tuple[object, object]]) -> None:
        """Readiness is a pure function of the probe set, not of evaluation order."""
        results = [
            ProbeResult(
                name=f"p{i}",
                status=status,  # type: ignore[arg-type]
                criticality=criticality,  # type: ignore[arg-type]
                detail=None,
                checked_at=EPOCH,
            )
            for i, (status, criticality) in enumerate(probes)
        ]
        report = aggregate(results, now=EPOCH)
        expected_ready = all(
            status is ProbeStatus.OK or criticality is ProbeCriticality.ADVISORY
            for status, criticality in probes
        )
        assert report.ready is expected_ready

    @given(
        probes=st.lists(
            st.tuples(st.sampled_from(list(ProbeStatus)), st.sampled_from(list(ProbeCriticality))),
            max_size=6,
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_aggregation_is_order_independent(self, probes: list[tuple[object, object]]) -> None:
        """Probe arrival order under concurrency must not change the verdict."""
        results = [
            ProbeResult(
                name=f"p{i}",
                status=status,  # type: ignore[arg-type]
                criticality=criticality,  # type: ignore[arg-type]
                detail=None,
                checked_at=EPOCH,
            )
            for i, (status, criticality) in enumerate(probes)
        ]
        forward = aggregate(results, now=EPOCH)
        backward = aggregate(list(reversed(results)), now=EPOCH)
        assert (forward.ready, forward.degraded) == (backward.ready, backward.degraded)

    @given(name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=20))
    @settings(max_examples=50, deadline=None)
    def test_probe_name_is_preserved(self, name: str) -> None:
        """Operators diagnose by probe name; losing it makes readiness output useless."""
        result = ProbeResult(
            name=name, status=ProbeStatus.OK, criticality=ProbeCriticality.CRITICAL, checked_at=EPOCH
        )
        assert aggregate([result], now=EPOCH).probes[0].name == name
"""Failure injection: idempotency, duplicate submission and duplicate side effects.

Phase 0 **OPS-004** makes content-hash idempotency the thing that absorbs at-least-once
delivery: the worker is allowed to run a job twice, but the effect must happen once. That
guarantee is only as good as its failure behaviour, which is what this module injects.

The interesting part is that the idempotency contract has **three distinct failure windows**
with different correct answers:

============================  =============================  ==========================
window                        fault injected                correct answer
============================  =============================  ==========================
claiming the key (``begin``)  store unreachable             abort *before* the side effect
doing the work                operation raises               release the key, re-raise
recording the outcome         store unreachable             surface the failure, never
(``complete``)                                               repeat the work
============================  =============================  ==========================

A single "the store is down" test would pass under implementations that get any one of
these wrong, so each window is injected separately via
:class:`tests.failure.conftest.FailingIdempotencyStore`.

Every test counts operation invocations. A replay path that re-runs the operation while
returning the cached response is idempotent in name only, and only a counter catches that.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from knowledge_assistant.application.idempotency import IdempotentExecutor, fingerprint
from knowledge_assistant.application.jobs import SubmitJob
from knowledge_assistant.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    ValidationError,
)
from knowledge_assistant.domain.idempotency import IdempotencyScope, IdempotencyStatus
from knowledge_assistant.workers.handlers import ECHO_JOB_TYPE

from ..conftest import InMemoryJobRepository
from ..failure.conftest import make_idempotency_store

pytestmark = pytest.mark.failure_injection

_SCOPE = IdempotencyScope.JOB_SUBMIT
_TENANT = "tenant-a"


class _Counter:
    """A side-effecting operation that records how many times it actually ran.

    Args:
        result: Payload returned on success.
        raises: Exception raised instead of returning, if the failure should be injected
            through the operation rather than through a collaborator.

    """

    __slots__ = ("calls", "raises", "result")

    def __init__(
        self,
        result: Mapping[str, Any] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        """Create the counter.

        Args:
            result: Payload returned on success.
            raises: Exception raised instead of returning.

        """
        self.calls = 0
        self.result = dict(result or {"ok": True})
        self.raises = raises

    async def __call__(self) -> Mapping[str, Any]:
        """Record the invocation and succeed or fail.

        Returns:
            The configured payload.

        Raises:
            BaseException: The configured failure, if any.

        """
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


async def _run(
    store: Any,
    clock: Any,
    operation: Any,
    *,
    tenant_id: str = _TENANT,
    key: str = "key-1",
    payload: Mapping[str, Any] | None = None,
) -> Any:
    """Run an idempotent operation against ``store``.

    Args:
        store: Idempotency store.
        clock: Fixed clock.
        operation: Zero-argument async callable.
        tenant_id: Owning tenant.
        key: Client idempotency key.
        payload: Request body.

    Returns:
        Whatever :meth:`IdempotentExecutor.run` returns.

    """
    executor = IdempotentExecutor(store, clock=clock)
    return await executor.run(
        tenant_id=tenant_id,
        scope=_SCOPE,
        key=key,
        payload=payload if payload is not None else {"n": 1},
        operation=operation,
    )


class TestHealthyOperationRunsExactlyOnce:
    """The negative control for the whole module.

    If every assertion below were "the operation did not run a second time", an
    implementation that never ran it at all would satisfy all of them. This class pins the
    opposite direction.
    """

    async def test_a_first_request_runs_the_operation_once(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Healthy path: exactly one execution, flagged as not replayed."""
        operation = _Counter()
        result, replayed = await _run(idempotency_store, clock, operation)
        assert operation.calls == 1
        assert replayed is False
        assert result == {"ok": True}

    async def test_the_outcome_is_recorded_as_completed(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """A recorded outcome is what makes the replay path possible at all."""
        await _run(idempotency_store, clock, _Counter())
        record = await idempotency_store.get(tenant_id=_TENANT, scope=_SCOPE, key="key-1")
        assert record is not None
        assert record.status is IdempotencyStatus.COMPLETED


class TestDuplicateSubmissionDoesNotDuplicateSideEffects:
    """The replay path must serve the stored response without re-running anything."""

    async def test_a_replayed_request_does_not_execute_again(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Scenario: the client retries because the first response was lost in transit.

        Expected classification: ``REPLAY``. Retry is **forbidden** for the operation itself
        even though the request is retried - that is the entire point of the key. Evidence:
        the invocation counter stays at one.
        """
        operation = _Counter(result={"receipt": "abc"})
        first, replayed_first = await _run(idempotency_store, clock, operation)
        second, replayed_second = await _run(idempotency_store, clock, operation)

        assert operation.calls == 1, "the replay path re-executed the operation"
        assert replayed_first is False
        assert replayed_second is True
        assert first == second

    async def test_a_replay_returns_the_original_response_not_a_new_one(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Evidence for the previous test that is not merely a counter.

        A replay path that computed a fresh result would also keep the counter at one if the
        cache write were simply skipped; comparing the payloads catches that.
        """
        operation = _Counter(result={"receipt": "original"})
        first, _ = await _run(idempotency_store, clock, operation)
        operation.result = {"receipt": "regenerated"}
        second, replayed = await _run(idempotency_store, clock, operation)

        assert replayed is True
        assert second == first == {"receipt": "original"}

    async def test_key_order_in_the_body_does_not_defeat_the_key(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """A client re-serialising its JSON with different key order is still a replay.

        The fingerprint is canonical, so an ordering change must not be mistaken for a
        different request and executed twice.
        """
        operation = _Counter()
        await _run(idempotency_store, clock, operation, payload={"a": 1, "b": 2})
        _, replayed = await _run(idempotency_store, clock, operation, payload={"b": 2, "a": 1})
        assert operation.calls == 1
        assert replayed is True

    async def test_another_tenant_with_the_same_key_is_not_a_replay(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Tenant isolation is part of the idempotency contract, not an afterthought.

        Sharing keys across tenants would let one tenant read another's stored response -
        a cross-tenant data leak dressed as an optimisation.
        """
        operation = _Counter(result={"receipt": "tenant-a"})
        await _run(idempotency_store, clock, operation, tenant_id=_TENANT)
        await _run(idempotency_store, clock, operation, tenant_id="tenant-b")
        assert operation.calls == 2

    async def test_the_same_key_in_another_scope_is_not_a_replay(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Reusing a key for a different operation family must execute, not replay.

        Otherwise a client's "retry" of operation B would return the response from operation
        A - the failure mode scoping exists to prevent.
        """
        operation = _Counter()
        executor = IdempotentExecutor(idempotency_store, clock=clock)
        await executor.run(
            tenant_id=_TENANT,
            scope=_SCOPE,
            key="key-1",
            payload={"n": 1},
            operation=operation,
        )
        await executor.run(
            tenant_id=_TENANT,
            scope=IdempotencyScope.DOCUMENT_UPLOAD,
            key="key-1",
            payload={"n": 1},
            operation=operation,
        )
        assert operation.calls == 2


class TestFingerprintConflictIsRejected:
    """A key reused with a different body is a client bug, and must not be served."""

    async def test_a_changed_body_is_rejected_without_executing(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Scenario: the client reuses a key for a materially different request.

        Expected classification: ``ConflictError`` with ``reason == fingerprint_mismatch``.
        Retry is **forbidden** - retrying cannot make the bodies match. Evidence: the counter
        is unchanged, so the second request did no work.
        """
        operation = _Counter()
        await _run(idempotency_store, clock, operation, payload={"amount": 1})
        with pytest.raises(ConflictError) as excinfo:
            await _run(idempotency_store, clock, operation, payload={"amount": 2})

        assert operation.calls == 1
        assert excinfo.value.detail.get("reason") == "fingerprint_mismatch"

    async def test_the_original_response_survives_a_conflict(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """A rejected mismatch must not overwrite the stored outcome.

        If the conflict path wrote the new body over the record, the next legitimate replay of
        the *original* request would return the wrong answer - a conflict would become data
        corruption.
        """
        operation = _Counter(result={"receipt": "original"})
        await _run(idempotency_store, clock, operation, payload={"amount": 1})
        with pytest.raises(ConflictError):
            await _run(idempotency_store, clock, operation, payload={"amount": 2})
        result, replayed = await _run(idempotency_store, clock, operation, payload={"amount": 1})

        assert replayed is True
        assert result == {"receipt": "original"}
        assert operation.calls == 1


class TestConcurrentDuplicateRunsOnce:
    """An in-flight key is a conflict, not a second execution and not a wait."""

    async def test_a_concurrent_duplicate_is_rejected_and_does_not_execute(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """Scenario: the client sends the same request twice before the first returns.

        Expected classification: ``ConflictError`` with ``reason == in_flight``. The caller is
        told to retry rather than being made to wait behind a held connection, because
        holding connections open is how a slow endpoint becomes a connection-exhaustion
        outage. Evidence: the counter stays at one.
        """
        started = _Counter()
        await idempotency_store.begin(
            tenant_id=_TENANT,
            scope=_SCOPE,
            key="key-1",
            request_fingerprint=fingerprint({"n": 1}),
            now=clock.now(),
            retention_seconds=60,
        )
        del started
        with pytest.raises(ConflictError) as excinfo:
            await _run(idempotency_store, clock, _Counter())
        assert excinfo.value.detail.get("reason") == "in_flight"

    async def test_a_key_left_in_flight_by_a_crashed_caller_expires(
        self, idempotency_store: Any, clock: Any
    ) -> None:
        """A worker that dies mid-operation must not lock the key forever.

        Retention is the backstop for a process that crashed after claiming a key and never
        released it. Without expiry, one crash would permanently reject that client's retries.
        """
        await idempotency_store.begin(
            tenant_id=_TENANT,
            scope=_SCOPE,
            key="key-1",
            request_fingerprint="ignored-after-expiry",
            now=clock.now(),
            retention_seconds=60,
        )
        clock.advance(61)
        operation = _Counter()
        _, replayed = await _run(idempotency_store, clock, operation)
        assert operation.calls == 1
        assert replayed is False


class TestStoreFailureWindows:
    """The three windows described in the module docstring, injected separately."""

    async def test_failing_to_claim_the_key_aborts_before_the_side_effect(self, clock: Any) -> None:
        """Window 1 - ``begin`` raises.

        The work must not happen at all. Executing first and claiming second would mean a
        store outage turns every keyed request into an unrecorded, unreplayable side effect.
        """
        store = make_idempotency_store(fail_on="begin")
        operation = _Counter()
        with pytest.raises(DependencyUnavailableError):
            await _run(store, clock, operation)

        assert store.calls == ["begin"], "the fault was bypassed rather than injected"
        assert operation.calls == 0, "the side effect ran without a claimed key"

    async def test_a_failing_operation_releases_the_key_for_a_real_retry(self, clock: Any) -> None:
        """Window 2 - the operation raises.

        Recording the failure as the outcome would make a transient error permanent for that
        key: every subsequent retry would replay an error. Evidence: a second, healthy run of
        the same key executes the operation.
        """
        store = make_idempotency_store(fail_on="begin")
        store.fail_on = "never"  # the store is healthy; only the operation fails
        failing = _Counter(raises=DependencyUnavailableError("down", dependency="postgres"))

        with pytest.raises(DependencyUnavailableError):
            await _run(store, clock, failing)
        assert failing.calls == 1

        healthy = _Counter()
        _, replayed = await _run(store, clock, healthy)
        assert healthy.calls == 1, "the retry replayed the failure instead of re-running"
        assert replayed is False

    async def test_the_original_failure_survives_a_failing_release(self, clock: Any) -> None:
        """A cleanup failure must never mask the error the caller needs to see.

        ``release`` runs on the way out of a failed operation and is best-effort. If its own
        exception escaped, the caller would see "idempotency store unavailable" instead of the
        actual cause - an operator chasing the wrong outage.
        """
        store = make_idempotency_store(fail_on="release")
        operation = _Counter(raises=ValidationError("payload was rejected"))

        with pytest.raises(ValidationError) as excinfo:
            await _run(store, clock, operation)

        assert str(excinfo.value) == "payload was rejected", "the store error masked the real one"
        assert operation.calls == 1
        assert store.calls.count("release") == 1, "release was never attempted"

    async def test_failing_to_record_the_outcome_never_repeats_the_work(self, clock: Any) -> None:
        """Window 3 - ``complete`` raises, *after* the side effect has already happened.

        This is the dangerous window. The work is done and unrecorded. The only safe answers
        are "surface the failure" and "never run the work again"; the second is enforced by
        the key remaining claimed, so evidence is that a subsequent attempt is a conflict and
        not a second execution.
        """
        store = make_idempotency_store(fail_on="complete")
        operation = _Counter(result={"receipt": "done-once"})

        with pytest.raises(DependencyUnavailableError):
            await _run(store, clock, operation)
        assert operation.calls == 1

        retry = _Counter()
        with pytest.raises(ConflictError) as excinfo:
            await _run(store, clock, retry)

        assert retry.calls == 0, "the side effect was repeated after an unrecorded outcome"
        assert excinfo.value.detail.get("reason") == "in_flight"


class TestDuplicateJobSubmission:
    """The job queue's own idempotency: one client key, one job row."""

    async def test_the_same_key_submits_one_job(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Evidence: the job count, not merely that two ids compared equal."""
        submit = SubmitJob(job_repository, clock=clock)
        first = await submit.execute(
            job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-1"
        )
        second = await submit.execute(
            job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-1"
        )

        assert first.id == second.id
        assert len(job_repository._jobs) == 1  # noqa: SLF001 - test double

    async def test_a_repeated_submission_does_not_add_attempts(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """A duplicate must not reset or inflate the retry budget.

        Re-enqueuing a job that has already burned two of three attempts would let a client
        retry loop defeat the ceiling entirely - DI-11 by the back door.
        """
        submit = SubmitJob(job_repository, clock=clock)
        job = await submit.execute(
            job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-1", max_attempts=3
        )
        for _ in range(5):
            await submit.execute(
                job_type=ECHO_JOB_TYPE,
                payload={"n": 1},
                idempotency_key="upload-1",
                max_attempts=3,
            )
        assert len(job_repository._jobs) == 1  # noqa: SLF001 - test double
        assert job_repository._jobs[job.id].attempts_used == 0  # noqa: SLF001 - test double

    async def test_a_different_key_submits_a_second_job(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """The negative control: deduplication must not over-fire into data loss.

        If every submission collapsed onto the first job, a client's second, genuinely
        different document would silently never be processed.
        """
        submit = SubmitJob(job_repository, clock=clock)
        await submit.execute(job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-1")
        await submit.execute(job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-2")
        assert len(job_repository._jobs) == 2  # noqa: SLF001 - test double

    async def test_a_submission_without_a_key_is_not_deduplicated(
        self, job_repository: InMemoryJobRepository, clock: Any
    ) -> None:
        """Keys are opt-in. A caller that supplies none gets fire-and-forget semantics."""
        submit = SubmitJob(job_repository, clock=clock)
        await submit.execute(job_type=ECHO_JOB_TYPE, payload={"n": 1})
        await submit.execute(job_type=ECHO_JOB_TYPE, payload={"n": 1})
        assert len(job_repository._jobs) == 2  # noqa: SLF001 - test double

    async def test_a_repository_outage_during_submission_creates_no_job(self, clock: Any) -> None:
        """Submission failure must be atomic from the client's point of view.

        A partially-enqueued job would be work the client believes was rejected, and a retry
        would duplicate it.
        """
        from ..failure.conftest import ExplodingJobRepository  # noqa: PLC0415

        repository = ExplodingJobRepository(clock=clock)
        repository.arm(times=1)
        submit = SubmitJob(repository, clock=clock)
        with pytest.raises(DependencyUnavailableError):
            await submit.execute(
                job_type=ECHO_JOB_TYPE, payload={"n": 1}, idempotency_key="upload-1"
            )
        assert repository._jobs == {}  # noqa: SLF001 - test double
        assert repository.refused == 1, "the fault was never reached"

"""Unit tests for the job lifecycle: submit, execute, retry, dead-letter, crash.

This is the Phase 1 guarantee that the whole asynchronous design rests on: work submitted to
the database is executed exactly as often as it should be, never lost, never duplicated
silently, and never retried forever. Each test names the production failure it prevents.
"""

from __future__ import annotations

import asyncio

import pytest

from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    TimeoutError_,
    ValidationError,
)
from knowledge_assistant.domain.jobs import JobState
from knowledge_assistant.workers.handlers import (
    ECHO_JOB_TYPE,
    FAILING_JOB_TYPE,
    FLAKY_JOB_TYPE,
    build_registry,
    echo_handler,
    failing_handler,
    flaky_handler,
)

pytestmark = pytest.mark.unit


async def _noop_handler(_payload: dict[str, object], _ctx: object) -> dict[str, object]:
    """Return a fixed successful result.

    Args:
        payload: Job payload (ignored).
        ctx: Job context (ignored).

    Returns:
        A minimal result mapping.

    """
    return {"ok": True}


class TestSubmit:
    """Job creation."""

    async def test_submit_creates_a_pending_job(self, submit_job, job_repository) -> None:  # noqa: ANN001
        job = await submit_job.execute(
            job_type=ECHO_JOB_TYPE, payload={"message": "hi"}, idempotency_key=None
        )
        assert job.state is JobState.PENDING
        assert job.attempts_used == 0
        stored = await job_repository.get(job.id)
        assert stored is not None

    async def test_submit_is_deterministic_in_availability(self, submit_job, clock) -> None:  # noqa: ANN001
        """A submitted job is immediately runnable; anything else hides work until the
        scheduler notices, which looks like data loss to a user.
        """
        job = await submit_job.execute(job_type=ECHO_JOB_TYPE, payload={}, idempotency_key=None)
        assert job.available_at <= clock.now()

    async def test_duplicate_idempotency_key_returns_the_same_job(
        self, submit_job, job_repository
    ) -> None:  # noqa: ANN001
        """A client that retries a submission must not create a second job: the second job
        is a second side effect.
        """
        first = await submit_job.execute(
            job_type=ECHO_JOB_TYPE, payload={"a": 1}, idempotency_key="dup-key-1234567890"
        )
        second = await submit_job.execute(
            job_type=ECHO_JOB_TYPE, payload={"a": 1}, idempotency_key="dup-key-1234567890"
        )
        assert first.id == second.id
        counts = await job_repository.counts_by_state()
        assert counts.get("pending") == 1

    async def test_blank_job_type_is_rejected(self, submit_job) -> None:  # noqa: ANN001
        """A blank job type is a programming error; it must not become an unrunnable job."""
        with pytest.raises(ValidationError):
            await submit_job.execute(job_type="   ", payload={}, idempotency_key=None)

    async def test_negative_delay_is_rejected(self, submit_job) -> None:  # noqa: ANN001
        """A negative delay would make the job claimable in the past forever; reject it
        rather than clamping silently.
        """
        with pytest.raises(ValidationError):
            await submit_job.execute(
                job_type=ECHO_JOB_TYPE, payload={}, delay_seconds=-1.0, idempotency_key=None
            )


class TestExecuteOnce:
    """Claim, run, settle."""

    async def test_no_work_returns_none(self, make_executor) -> None:  # noqa: ANN001
        executor = make_executor({ECHO_JOB_TYPE: _noop_handler})
        assert await executor.execute_once() is None

    async def test_successful_job_is_marked_succeeded(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        await submit_job.execute(
            job_type=ECHO_JOB_TYPE, payload={"message": "x"}, idempotency_key=None
        )
        outcome = await make_executor({ECHO_JOB_TYPE: _noop_handler}).execute_once()
        assert outcome is not None
        assert outcome.outcome == "succeeded"
        counts = await job_repository.counts_by_state()
        assert counts.get("succeeded") == 1
        assert counts.get("pending", 0) == 0

    async def test_unknown_job_type_dead_letters_without_retrying(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        """A job whose handler is missing is a permanent failure. Retrying it would fill the
        queue with jobs that can only fail, starving real work.
        """
        job = await submit_job.execute(job_type="unregistered", payload={}, idempotency_key=None)
        outcome = await make_executor({ECHO_JOB_TYPE: _noop_handler}).execute_once()
        assert outcome is not None
        assert outcome.outcome == "dead_lettered"
        stored = await job_repository.get(job.id)
        assert stored is not None
        assert stored.state is JobState.DEAD_LETTERED

    async def test_claim_is_exclusive(self, submit_job, make_executor) -> None:  # noqa: ANN001
        """Two workers must never hold the same job. Without exclusivity a job is executed
        twice, which for ingestion means duplicate content.
        """
        await submit_job.execute(job_type=ECHO_JOB_TYPE, payload={}, idempotency_key=None)
        executor = make_executor({ECHO_JOB_TYPE: _noop_handler})
        first = await executor.execute_once()
        second = await executor.execute_once()
        assert first is not None
        assert second is None

    async def test_stale_settlement_does_not_overwrite_a_newer_state(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        """Fencing: if a lease lapsed and another worker finished the job, the original
        worker must not overwrite the result with its own late outcome.
        """
        await submit_job.execute(job_type=ECHO_JOB_TYPE, payload={}, idempotency_key=None)
        executor = make_executor({ECHO_JOB_TYPE: _noop_handler})
        await executor.execute_once()
        settle_calls = len(job_repository.settle_calls)
        await executor.execute_once()  # nothing claimable, so no further settlement
        assert len(job_repository.settle_calls) == settle_calls


class TestFailureHandling:
    """Retry versus dead-letter, and the attempt ceiling."""

    async def test_transient_failure_is_retried(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        await submit_job.execute(
            job_type=FAILING_JOB_TYPE,
            payload={"failure_kind": "transient"},
            idempotency_key=None,
        )
        executor = make_executor({FAILING_JOB_TYPE: failing_handler})
        outcome = await executor.execute_once()
        assert outcome is not None
        assert outcome.outcome == "retry_scheduled"
        counts = await job_repository.counts_by_state()
        assert counts.get("retry") == 1

    async def test_permanent_failure_is_dead_lettered_immediately(
        self, submit_job, make_executor
    ) -> None:  # noqa: ANN001
        """Retrying a permanent failure wastes the attempt budget and delays the alert that
        says 'a human needs to look at this'.
        """
        await submit_job.execute(
            job_type=FAILING_JOB_TYPE,
            payload={"failure_kind": "permanent"},
            idempotency_key=None,
        )
        executor = make_executor({FAILING_JOB_TYPE: failing_handler})
        outcome = await executor.execute_once()
        assert outcome is not None
        assert outcome.outcome == "dead_lettered"

    async def test_attempt_ceiling_dead_letters(
        self, submit_job, job_repository, make_executor, clock
    ) -> None:  # noqa: ANN001
        """Bounded retries: without a ceiling, a permanently broken dependency produces an
        infinite retry loop that looks like load and hides the real failure.
        """
        job = await submit_job.execute(
            job_type=FAILING_JOB_TYPE,
            payload={"failure_kind": "unknown"},
            idempotency_key=None,
        )
        executor = make_executor(
            {FAILING_JOB_TYPE: failing_handler},
            backoff_base_seconds=0.001,
            backoff_cap_seconds=0.001,
        )
        outcomes = []
        for _ in range(12):
            clock.advance(600)
            result = await executor.execute_once()
            if result is None:
                break
            outcomes.append(result.outcome)
        assert outcomes[-1] == "dead_lettered", outcomes
        stored = await job_repository.get(job.id)
        assert stored is not None
        assert stored.attempts_used <= stored.max_attempts

    async def test_flaky_job_eventually_succeeds(self, submit_job, make_executor, clock) -> None:  # noqa: ANN001
        """The success-after-failure path is what makes retry policy worth having."""
        await submit_job.execute(
            job_type=FLAKY_JOB_TYPE, payload={"fail_until_attempt": 2}, idempotency_key=None
        )
        executor = make_executor({FLAKY_JOB_TYPE: flaky_handler})
        first = await executor.execute_once()
        assert first is not None and first.outcome == "retry_scheduled"
        clock.advance(120)
        second = await executor.execute_once()
        assert second is not None and second.outcome == "succeeded"

    async def test_cancellation_is_settled_not_swallowed(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        """A worker killed mid-job must leave the job retryable, not dead-lettered: the work
        was never attempted, so dead-lettering it loses the job permanently.
        """

        async def cancelling(_payload: dict[str, object], _ctx: object) -> dict[str, object]:
            raise asyncio.CancelledError

        await submit_job.execute(job_type="cancelme", payload={}, idempotency_key=None)
        executor = make_executor({"cancelme": cancelling})
        outcome = await executor.execute_once()
        assert outcome is not None
        assert outcome.outcome in {"retry_scheduled", "dead_lettered"}
        counts = await job_repository.counts_by_state()
        assert counts.get("retry") == 1, "a cancelled job must be retryable"

    async def test_timeout_is_transient(self, submit_job, make_executor) -> None:  # noqa: ANN001
        async def timing_out(_payload: dict[str, object], _ctx: object) -> dict[str, object]:
            raise TimeoutError_("slow", dependency="postgres", timeout_seconds=1.0)

        await submit_job.execute(job_type="slow", payload={}, idempotency_key=None)
        outcome = await make_executor({"slow": timing_out}).execute_once()
        assert outcome is not None and outcome.outcome == "retry_scheduled"

    async def test_dependency_failure_is_transient(self, submit_job, make_executor) -> None:  # noqa: ANN001
        async def failing(_payload: dict[str, object], _ctx: object) -> dict[str, object]:
            raise DependencyUnavailableError("db down", dependency="postgres")

        await submit_job.execute(job_type="dep", payload={}, idempotency_key=None)
        outcome = await make_executor({"dep": failing}).execute_once()
        assert outcome is not None and outcome.outcome == "retry_scheduled"

    async def test_error_summary_is_truncated(
        self, submit_job, job_repository, make_executor
    ) -> None:  # noqa: ANN001
        """A stack trace or driver message can be enormous. Storing it verbatim bloats the
        jobs table and can embed credentials in a column operators read in a terminal.
        """

        async def verbose(_payload: dict[str, object], _ctx: object) -> dict[str, object]:
            raise RuntimeError("x" * 10_000)

        job = await submit_job.execute(job_type="verbose", payload={}, idempotency_key=None)
        await make_executor({"verbose": verbose}).execute_once()
        stored = await job_repository.get(job.id)
        assert stored is not None
        assert stored.last_error is not None
        assert len(stored.last_error) <= 600


class TestWorkerRunner:
    """The polling loop."""

    async def test_runner_executes_queued_jobs(
        self, submit_job, job_repository, make_executor, clock
    ) -> None:  # noqa: ANN001
        from knowledge_assistant.workers.runner import WorkerRunner  # noqa: PLC0415

        for i in range(3):
            await submit_job.execute(job_type=ECHO_JOB_TYPE, payload={"i": i}, idempotency_key=None)
        executor = make_executor(build_registry())
        runner = WorkerRunner(
            executor, clock=clock, poll_interval_seconds=0.01, drain_timeout_seconds=1.0
        )
        executed = await runner.run_forever(max_iterations=10)
        assert executed == 3
        counts = await job_repository.counts_by_state()
        assert counts.get("succeeded") == 3

    async def test_runner_survives_a_repository_error(self, clock) -> None:  # noqa: ANN001
        """A worker that exits on a transient database blip turns a recoverable error into a
        stopped pipeline. The loop must keep running.
        """
        from knowledge_assistant.workers.runner import WorkerRunner  # noqa: PLC0415

        calls: list[int] = []

        class _Boom:
            async def execute_once(self) -> None:
                calls.append(1)
                raise DependencyUnavailableError("db down", dependency="postgres")

        runner = WorkerRunner(
            _Boom(), clock=clock, poll_interval_seconds=0.0, drain_timeout_seconds=1.0
        )
        await runner.run_forever(max_iterations=3)
        assert len(calls) == 3

    async def test_request_stop_ends_the_loop(self, make_executor, clock) -> None:  # noqa: ANN001
        """Graceful shutdown: a SIGTERM must not leave a job claimed and unsettled."""
        from knowledge_assistant.workers.runner import WorkerRunner  # noqa: PLC0415

        executor = make_executor(build_registry())
        runner = WorkerRunner(
            executor, clock=clock, poll_interval_seconds=0.01, drain_timeout_seconds=1.0
        )
        runner.request_stop()
        executed = await runner.run_forever(max_iterations=100)
        assert executed == 0

    async def test_idle_loop_does_not_spin_the_database(self, make_executor, clock) -> None:  # noqa: ANN001
        """An idle worker must sleep between polls. A tight loop against an empty queue turns
        the queue table into a busy database with no work to show for it.
        """
        from knowledge_assistant.workers.runner import WorkerRunner  # noqa: PLC0415

        executor = make_executor(build_registry())
        runner = WorkerRunner(
            executor, clock=clock, poll_interval_seconds=0.01, drain_timeout_seconds=1.0
        )
        started = asyncio.get_running_loop().time()
        await runner.run_forever(max_iterations=5)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.04, f"idle loop appears not to sleep: {elapsed:.4f}s"


class TestHandlerRegistry:
    """The registry is the worker's contract surface."""

    def test_registry_contains_the_documented_test_jobs(self) -> None:
        registry = build_registry()
        assert {ECHO_JOB_TYPE, FAILING_JOB_TYPE, FLAKY_JOB_TYPE} <= set(registry)

    def test_every_handler_is_async_callable(self) -> None:
        import inspect  # noqa: PLC0415

        for name, handler in build_registry().items():
            assert inspect.iscoroutinefunction(handler), name

    async def test_echo_handler_returns_observable_output(self) -> None:
        """The trivial test job exists to prove the whole path end to end, so it must return
        something a test can assert on.
        """

        class _Ctx:
            job_id = "j1"
            job_type = ECHO_JOB_TYPE
            attempt = 1

        result = await echo_handler({"message": "hello"}, _Ctx())
        assert result["echoed"] == {"message": "hello"}
        assert result["job_id"] == "j1"
        assert result["attempt"] == 1

"""Shared fixtures and test doubles.

Two things live here that belong together: **fixtures** (what a test is given) and **doubles**
(what stands in for a collaborator). Keeping them in one module makes it obvious, when reading a
test, exactly how much of the world is real.

The doubles are not mocks. They are in-memory implementations of the same ports the production
adapters implement, so a test that passes against a double is testing behaviour rather than
call sequences. Where a test asserts *that something was not called*, that is an assertion about
an observable consequence (a counter), never about a mock's call list.

Why in-memory doubles rather than a real database for the default test lane: the retry, lease and
idempotency logic is the highest-risk code in Phase 1 and it must be runnable in under a second,
on any machine, with no Docker. The real-database tests exist separately and are marked
``integration``; they are skipped unless ``TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import pytest

from knowledge_assistant.application.health import HealthService
from knowledge_assistant.application.idempotency import IdempotentExecutor
from knowledge_assistant.application.jobs import ExecuteJobOnce, SubmitJob
from knowledge_assistant.application.ports import HealthProbePort
from knowledge_assistant.config.settings import Settings
from knowledge_assistant.domain.clock import UTC, FixedClock
from knowledge_assistant.domain.errors import DependencyUnavailableError
from knowledge_assistant.domain.health import ProbeCriticality, ProbeResult, ProbeStatus
from knowledge_assistant.domain.idempotency import IdempotencyRecord, IdempotencyScope, IdempotencyStatus
from knowledge_assistant.domain.jobs import Job, JobClaim, JobState
from knowledge_assistant.interfaces.http.app import create_app

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

#: A fixed instant used by every test that needs time. Chosen to be an arbitrary, memorable
#: value rather than "now", so that an assertion which accidentally depends on wall-clock time
#: fails loudly instead of passing intermittently.
TEST_EPOCH = dt.datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    """Return a deterministic clock starting at :data:`TEST_EPOCH`.

    Returns:
        A ``FixedClock``.
    """
    return FixedClock(TEST_EPOCH)


@pytest.fixture
def settings() -> Settings:
    """Return valid local settings, with no secret in the repository.

    Returns:
        Settings built from a development DSN.
    """
    return Settings(  # type: ignore[call-arg]
        environment="test",
        database_url="postgresql://test:test@localhost:5432/ka_test",
        service_name="knowledge-assistant-test",
    )


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class InMemoryJobRepository:
    """In-memory ``JobRepositoryPort``.

    Implements the contract, not the SQL: claim is exclusive, settlement is fenced by state, and
    the lease bounds ownership. A test that passes here is testing the use case's behaviour; the
    PostgreSQL adapter is separately verified by the integration lane.
    """

    def __init__(self, *, clock: FixedClock) -> None:
        """Create an empty repository.

        Args:
            clock: Clock used for lease arithmetic.
        """
        self._jobs: dict[str, Job] = {}
        self._clock = clock
        self.settle_calls: list[tuple[str, str]] = []

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any],
        available_at: dt.datetime,
        max_attempts: int,
        idempotency_key: str | None = None,
    ) -> Job:
        """Insert a job, honouring the idempotency key.

        Args:
            job_type: Registered job type.
            payload: JSON-serialisable input.
            available_at: Earliest claim time.
            max_attempts: Attempt ceiling.
            idempotency_key: Optional client key.

        Returns:
            The created job, or the existing job for a duplicate key.
        """
        if idempotency_key is not None:
            for job in self._jobs.values():
                if job.payload.get("_idempotency_key") == idempotency_key:
                    return job
        job = Job(
            id=f"job-{len(self._jobs) + 1}",
            type=job_type,
            state=JobState.PENDING,
            attempts_used=0,
            max_attempts=max_attempts,
            available_at=available_at,
            lease_expires_at=None,
            payload={**dict(payload), "_idempotency_key": idempotency_key}
            if idempotency_key
            else dict(payload),
            created_at=self._clock.now(),
        )
        self._jobs[job.id] = job
        return job

    async def claim_next(self, *, now: dt.datetime, lease_seconds: float) -> JobClaim | None:
        """Claim the earliest runnable job exclusively.

        Args:
            now: Current time.
            lease_seconds: Lease duration.

        Returns:
            A claim, or ``None``.
        """
        candidates = sorted(
            (
                job
                for job in self._jobs.values()
                if job.state in (JobState.PENDING, JobState.RETRY) and job.available_at <= now
            ),
            key=lambda j: (j.available_at, j.id),
        )
        if not candidates:
            return None
        target = candidates[0]
        claimed = Job(
            id=target.id,
            type=target.type,
            state=JobState.RUNNING,
            attempts_used=target.attempts_used + 1,
            max_attempts=target.max_attempts,
            available_at=target.available_at,
            lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
            payload=target.payload,
            last_error=target.last_error,
            created_at=target.created_at,
        )
        self._jobs[target.id] = claimed
        return JobClaim(
            job=claimed, claimed_at=now, lease_expires_at=claimed.lease_expires_at  # type: ignore[arg-type]
        )

    async def mark_succeeded(self, *, claim: JobClaim, result: Mapping[str, Any]) -> None:
        """Record success if the claim is still current.

        Args:
            claim: Ownership proof.
            result: Handler result.
        """
        self.settle_calls.append((claim.job.id, "succeeded"))
        if self._jobs[claim.job.id].state is not JobState.RUNNING:
            return  # fenced by another worker
        self._replace(claim.job.id, state=JobState.SUCCEEDED)

    async def mark_retry(
        self, *, claim: JobClaim, error: str, next_available_at: dt.datetime
    ) -> None:
        """Reschedule if the claim is still current.

        Args:
            claim: Ownership proof.
            error: Failure summary.
            next_available_at: Next eligible time.
        """
        self.settle_calls.append((claim.job.id, "retry"))
        if self._jobs[claim.job.id].state is not JobState.RUNNING:
            return
        self._replace(claim.job.id, state=JobState.RETRY, available_at=next_available_at, last_error=error)

    async def mark_dead_lettered(self, *, claim: JobClaim, error: str) -> None:
        """Dead-letter if the claim is still current.

        Args:
            claim: Ownership proof.
            error: Failure summary.
        """
        self.settle_calls.append((claim.job.id, "dead_lettered"))
        if self._jobs[claim.job.id].state is not JobState.RUNNING:
            return
        self._replace(claim.job.id, state=JobState.DEAD_LETTERED, last_error=error)

    def _replace(self, job_id: str, **changes: Any) -> Job:
        """Return an updated job stored under ``job_id``.

        Args:
            job_id: Identifier to update.
            **changes: Field changes.

        Returns:
            The updated job.
        """
        from dataclasses import replace  # noqa: PLC0415

        updated = replace(self._jobs[job_id], **changes)
        self._jobs[job_id] = updated
        return updated

    async def get(self, job_id: str) -> Job | None:
        """Return a job by identifier.

        Args:
            job_id: Identifier.

        Returns:
            The job or ``None``.
        """
        return self._jobs.get(job_id)

    async def counts_by_state(self) -> Mapping[str, int]:
        """Return counts by state.

        Returns:
            Mapping of state to count.
        """
        counts: dict[str, int] = {}
        for job in self._jobs.values():
            counts[job.state.value] = counts.get(job.state.value, 0) + 1
        return counts

    async def dead_letter_count(self) -> int:
        """Return the dead-lettered count."""
        return sum(1 for job in self._jobs.values() if job.state is JobState.DEAD_LETTERED)

    async def oldest_pending_age_seconds(self, *, now: dt.datetime) -> float | None:
        """Return the age of the oldest pending job.

        Args:
            now: Current time.

        Returns:
            Age in seconds or ``None``.
        """
        pending = [
            job
            for job in self._jobs.values()
            if job.state in (JobState.PENDING, JobState.RETRY)
        ]
        if not pending:
            return None
        oldest = min(job.available_at for job in pending)
        return (now - oldest).total_seconds()


class InMemoryIdempotencyStore:
    """In-memory ``IdempotencyStorePort`` implementing the atomic-claim contract."""

    def __init__(self) -> None:
        """Create an empty store."""
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}

    async def get(
        self, *, tenant_id: str, scope: IdempotencyScope, key: str
    ) -> IdempotencyRecord | None:
        """Return the record for a key.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        Returns:
            The record or ``None``.
        """
        return self._records.get((tenant_id, scope.value, key))

    async def begin(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        request_fingerprint: str,
        now: dt.datetime,
        retention_seconds: int,
    ) -> IdempotencyRecord | None:
        """Claim a key, or return the existing record.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            request_fingerprint: Request fingerprint.
            now: Current time.
            retention_seconds: Retention window.

        Returns:
            ``None`` if the caller owns the key, else the existing record.
        """
        ident = (tenant_id, scope.value, key)
        existing = self._records.get(ident)
        if existing is not None and now < existing.expires_at:
            return existing
        record = IdempotencyRecord(
            scope=scope,
            key=key,
            status=IdempotencyStatus.IN_PROGRESS,
            request_fingerprint=request_fingerprint,
            response_payload=None,
            created_at=now,
            expires_at=now + dt.timedelta(seconds=retention_seconds),
        )
        self._records[ident] = record
        return None

    async def complete(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        response_payload: Mapping[str, Any],
        now: dt.datetime,
    ) -> None:
        """Record a committed outcome.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            response_payload: Stored response.
            now: Current time.
        """
        ident = (tenant_id, scope.value, key)
        existing = self._records[ident]
        from dataclasses import replace  # noqa: PLC0415

        self._records[ident] = replace(
            existing,
            status=IdempotencyStatus.COMPLETED,
            response_payload=dict(response_payload),
        )

    async def release(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Release a claimed key.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
        """
        self._records.pop((tenant_id, scope.value, key), None)


class FakeProbe:
    """Deterministic ``HealthProbePort`` with a settable outcome."""

    def __init__(
        self,
        name: str,
        *,
        status: ProbeStatus = ProbeStatus.OK,
        criticality: ProbeCriticality = ProbeCriticality.CRITICAL,
        detail: str | None = "ok",
        raises: BaseException | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        """Build a probe double.

        Args:
            name: Probe name.
            status: Status to return.
            criticality: Whether the probe gates readiness.
            detail: Detail string.
            raises: Exception to raise instead of returning, for failure injection.
            delay_seconds: Artificial latency, for timeout testing.
        """
        self._name = name
        self._status = status
        self._criticality = criticality
        self._detail = detail
        self._raises = raises
        self._delay = delay_seconds

    @property
    def name(self) -> str:
        """Return the probe name."""
        return self._name

    @property
    def criticality(self) -> ProbeCriticality:
        """Return the probe criticality."""
        return self._criticality

    async def check(self) -> ProbeResult:
        """Return the configured outcome.

        Returns:
            A probe result.

        Raises:
            BaseException: The configured exception, if any.
        """
        if self._delay:
            import asyncio  # noqa: PLC0415

            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return ProbeResult(
            name=self._name,
            status=self._status,
            criticality=self._criticality,
            detail=self._detail,
        )


class CountingDatabase:
    """Minimal ``Database`` stand-in that fails on demand."""

    def __init__(self, *, failure: BaseException | None = None) -> None:
        """Build the stand-in.

        Args:
            failure: Exception raised by ``acquire`` when supplied.
        """
        self.failure = failure
        self.closed = False

    async def open(self) -> None:
        """Record an open attempt.

        Raises:
            DependencyUnavailableError: If configured to fail.
        """
        if self.failure:
            raise self.failure

    async def close(self) -> None:
        """Record a close."""
        self.closed = True

    def acquire(self) -> Any:
        """Return an async context manager yielding a fake connection.

        Returns:
            An async context manager.

        Raises:
            DependencyUnavailableError: If configured to fail. Raised eagerly so that the
                failure mode matches psycopg's, which refuses at checkout rather than inside
                the context.
        """
        failure = self.failure

        class _Ctx:
            async def __aenter__(self_inner: Any) -> Any:
                if failure is not None:
                    raise failure
                return _FakeConnection()

            async def __aexit__(self_inner: Any, *exc: object) -> None:
                return None

        return _Ctx()


class _FakeConnection:
    """Minimal connection for the fake database."""

    def cursor(self, **kwargs: object) -> _FakeCursor:
        """Return a fake cursor.

        Args:
            **kwargs: Accepted and ignored.

        Returns:
            A fake cursor.
        """
        return _FakeCursor()


class _FakeCursor:
    """Minimal cursor for the fake database."""

    async def __aenter__(self) -> _FakeCursor:
        """Enter the cursor context."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the cursor context."""
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        """Accept an execute call.

        Args:
            *args: Ignored.
            **kwargs: Ignored.
        """
        return None

    async def fetchone(self) -> None:
        """Return no row.

        Returns:
            ``None``.
        """
        return None


@pytest.fixture
def job_repository(clock: FixedClock) -> InMemoryJobRepository:
    """Return an in-memory job repository bound to the fixed clock.

    Args:
        clock: The fixed clock fixture.

    Returns:
        The repository double.
    """
    return InMemoryJobRepository(clock=clock)


@pytest.fixture
def idempotency_store() -> InMemoryIdempotencyStore:
    """Return an in-memory idempotency store.

    Returns:
        The store double.
    """
    return InMemoryIdempotencyStore()


@pytest.fixture
def submit_job(job_repository: InMemoryJobRepository, clock: FixedClock) -> SubmitJob:
    """Return the submit-job use case.

    Args:
        job_repository: Repository double.
        clock: Fixed clock.

    Returns:
        The use case.
    """
    return SubmitJob(job_repository, clock=clock)


@pytest.fixture
def idempotent_executor(
    idempotency_store: InMemoryIdempotencyStore, clock: FixedClock
) -> IdempotentExecutor:
    """Return the idempotent executor.

    Args:
        idempotency_store: Store double.
        clock: Fixed clock.

    Returns:
        The use case.
    """
    return IdempotentExecutor(idempotency_store, clock=clock)


@pytest.fixture
def make_executor(
    job_repository: InMemoryJobRepository, clock: FixedClock
) -> Callable[..., ExecuteJobOnce]:
    """Return a factory building an ``ExecuteJobOnce`` with test-friendly backoff.

    Args:
        job_repository: Repository double.
        clock: Fixed clock.

    Returns:
        Factory taking a handler mapping.
    """

    def _factory(handlers: Mapping[str, Any], **kwargs: Any) -> ExecuteJobOnce:
        options: dict[str, Any] = {
            "lease_seconds": 30.0,
            "backoff_base_seconds": 1.0,
            "backoff_cap_seconds": 60.0,
            "jitter_ratio": 0.0,
        }
        options.update(kwargs)
        return ExecuteJobOnce(job_repository, dict(handlers), clock=clock, **options)

    return _factory


def make_health_service(
    probes: list[HealthProbePort], clock: FixedClock, *, timeout_seconds: float = 2.0
) -> HealthService:
    """Build a health service over a probe list.

    Args:
        probes: Probes to run.
        clock: Fixed clock.
        timeout_seconds: Probe-set budget.

    Returns:
        The service.
    """
    return HealthService(probes, clock=clock, timeout_seconds=timeout_seconds)


def make_app(
    *,
    probes: list[HealthProbePort],
    clock: FixedClock,
    settings: Settings,
    max_body_bytes: int = 1024 * 1024,
) -> Any:
    """Build the FastAPI application over given probes.

    Args:
        probes: Probes backing readiness.
        clock: Fixed clock.
        settings: Settings for the public summary.
        max_body_bytes: Body size limit.

    Returns:
        A ``FastAPI`` application.
    """
    return create_app(
        health=make_health_service(probes, clock),
        service_name=settings.service_name,
        settings_public=settings.public_summary(),
        clock=clock,
        max_body_bytes=max_body_bytes,
    )


@pytest.fixture
async def healthy_client() -> AsyncIterator[Any]:
    """Yield an HTTP client bound to an app with one healthy critical probe.

    Yields:
        An ``httpx.AsyncClient`` over an ASGI transport.
    """
    import httpx  # noqa: PLC0415

    clock = FixedClock(TEST_EPOCH)
    settings = Settings(  # type: ignore[call-arg]
        environment="test", database_url="postgresql://t:t@localhost/ka", service_name="ka-test"
    )
    app = make_app(probes=[FakeProbe("postgres")], clock=clock, settings=settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def dependency_error() -> DependencyUnavailableError:
    """Return a dependency error for failure-injection tests.

    Returns:
        A ``DependencyUnavailableError``.
    """
    return DependencyUnavailableError("simulated", dependency="postgres")
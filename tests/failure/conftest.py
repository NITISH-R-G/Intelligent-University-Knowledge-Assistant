"""Shared fixtures for the failure-injection lane.

Fault injection here is **deterministic by construction**. Nothing sleeps, nothing waits
for a real network, nothing depends on a container. A resilience suite whose results
change when the machine is busy cannot be used to decide whether a guarantee holds.

The existing doubles in ``tests/conftest.py`` already do most of the work - ``FakeProbe``
accepts a raised exception and a delay, ``InMemoryJobRepository`` counts settlements, and
``make_executor`` pins jitter to zero. What is added here is specific to fault injection:

* :class:`ExplodingJobRepository` - a repository that fails, so the worker's survival
  behaviour can be observed. The existing double cannot fail on demand.
* :class:`RecordingSleeper` - captures requested sleeps instead of performing them, so a
  backoff *schedule* can be asserted without waiting for it.
* :class:`FailingIdempotencyStore` - fails at one *named* store operation, so the three
  idempotency failure windows (claim, work, record) can be tested separately.
* :func:`scenario` - records the eight facts Phase 0 asks about a failure scenario, so
  each test states its own contract rather than leaving it implied.

Nothing here patches production internals. Every fault is injected through a port
boundary the production code already depends on.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any

import pytest

from knowledge_assistant.application.jobs import ExecuteJobOnce
from knowledge_assistant.domain.errors import DependencyUnavailableError
from knowledge_assistant.domain.idempotency import IdempotencyRecord, IdempotencyScope
from knowledge_assistant.domain.jobs import Job, JobClaim

from ..conftest import InMemoryIdempotencyStore, InMemoryJobRepository

pytestmark = pytest.mark.failure_injection


@dataclasses.dataclass(frozen=True, slots=True)
class Scenario:
    """The contract a failure-injection test defends.

    Attributes:
        component: What fails.
        injection: How the fault is introduced.
        expected_behaviour: What the system must do.
        expected_classification: The ``ErrorKind`` or failure class expected.
        retry_expected: Whether retrying is correct.
        retry_forbidden: Whether retrying must not happen.
        idempotency_required: Whether repeating the operation must be safe.
        evidence: The observable proof asserted by the test.

    """

    component: str
    injection: str
    expected_behaviour: str
    expected_classification: str
    retry_expected: bool
    retry_forbidden: bool
    idempotency_required: bool
    evidence: str


def scenario(**kwargs: Any) -> Scenario:
    """Build a :class:`Scenario` from keyword arguments.

    Args:
        **kwargs: The eight scenario fields.

    Returns:
        The scenario record.

    """
    return Scenario(**kwargs)


class ExplodingJobRepository(InMemoryJobRepository):
    """A ``JobRepositoryPort`` that fails on demand.

    Subclasses the in-memory double so every healthy path keeps working, and adds one
    capability the base class lacks: the ability to raise instead of answering. That is
    what makes worker-survival testable, because a repository that always succeeds can
    never demonstrate that the worker keeps running when one does not.
    """

    def __init__(self, *, clock: Any) -> None:
        """Create the repository with the fault disarmed.

        Args:
            clock: Clock used for lease arithmetic.

        """
        super().__init__(clock=clock)
        #: Exception raised by the next N repository calls, then the repository recovers.
        self.fail_times = 0
        #: The exception raised while the fault is armed.
        self.failure = DependencyUnavailableError(
            "simulated repository outage", dependency="postgres"
        )
        #: How many calls were refused, so a test can prove the fault was actually hit.
        self.refused = 0

    def arm(self, *, times: int = 1, failure: BaseException | None = None) -> None:
        """Make the next ``times`` calls raise.

        Args:
            times: Number of consecutive calls to fail.
            failure: Exception to raise. Defaults to a ``DependencyUnavailableError``.

        """
        self.fail_times = times
        if failure is not None:
            self.failure = failure

    def _maybe_fail(self) -> None:
        """Raise if the fault is armed.

        Raises:
            BaseException: The configured failure.

        """
        if self.fail_times > 0:
            self.fail_times -= 1
            self.refused += 1
            raise self.failure

    async def claim_next(self, *, now: Any, lease_seconds: float) -> JobClaim | None:
        """Claim a job, or fail if the fault is armed.

        Args:
            now: Current time.
            lease_seconds: Lease duration.

        Returns:
            A claim, or ``None``.

        """
        self._maybe_fail()
        return await super().claim_next(now=now, lease_seconds=lease_seconds)

    async def enqueue(self, **kwargs: Any) -> Job:
        """Enqueue a job, or fail if the fault is armed.

        Args:
            **kwargs: Passed through to the base implementation.

        Returns:
            The created job.

        """
        self._maybe_fail()
        return await super().enqueue(**kwargs)


class RecordingSleeper:
    """Captures requested sleeps instead of performing them.

    A worker that backs off for 300 seconds would make the suite take five minutes, and a
    suite that waits is a suite whose timing assertions are the real subject. Recording
    the requested durations lets a test assert the *schedule* - which is the part the
    architecture actually guarantees - with no wall-clock cost.
    """

    def __init__(self) -> None:
        """Create an empty recorder."""
        self.requested: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record a requested sleep without performing it.

        Args:
            seconds: Requested duration.

        """
        self.requested.append(seconds)


@pytest.fixture
def exploding_repository(clock: Any) -> ExplodingJobRepository:
    """Provide a repository double that can be told to fail.

    Args:
        clock: Fixed clock.

    Returns:
        The armed-on-demand repository.

    """
    return ExplodingJobRepository(clock=clock)


@pytest.fixture
def sleeper() -> RecordingSleeper:
    """Provide a sleep recorder.

    Returns:
        The recorder.

    """
    return RecordingSleeper()


def make_idempotency_store(*, fail_on: str) -> FailingIdempotencyStore:
    """Build an idempotency store whose named operation raises.

    A function rather than a fixture because the fault is a *parameter* of the scenario, not
    a property of the environment. Parametrising it directly keeps the failure window visible
    in the test name.

    Args:
        fail_on: Store method that should raise.

    Returns:
        The armed store double.

    """
    return FailingIdempotencyStore(fail_on=fail_on)


def build_executor(
    repository: Any,
    handlers: dict[str, Any],
    *,
    clock: Any,
    lease_seconds: float = 30.0,
    backoff_base_seconds: float = 1.0,
    backoff_cap_seconds: float = 60.0,
    jitter_ratio: float = 0.0,
) -> Any:
    """Wire an ``ExecuteJobOnce`` to a *specific* repository.

    The shared ``make_executor`` fixture closes over the ``job_repository`` fixture, which
    is convenient for tests about job behaviour and wrong for tests about repository
    failure: an executor built there would be attached to a healthy repository while the
    test armed a fault on a different one, and the test would silently prove nothing.

    Taking the repository as an argument removes that hidden coupling, so a failure test
    cannot accidentally exercise the wrong object.

    Jitter defaults to zero so backoff is exactly computable and the suite never waits.

    Args:
        repository: Job repository the executor will claim from.
        handlers: Handler registry.
        clock: Injected clock.
        lease_seconds: Lease granted on claim.
        backoff_base_seconds: First retry delay.
        backoff_cap_seconds: Retry delay ceiling.
        jitter_ratio: Jitter fraction; zero keeps the schedule deterministic.

    Returns:
        The executor.

    """
    return ExecuteJobOnce(
        repository,
        handlers,
        clock=clock,
        lease_seconds=lease_seconds,
        backoff_base_seconds=backoff_base_seconds,
        backoff_cap_seconds=backoff_cap_seconds,
        jitter_ratio=jitter_ratio,
    )


class FailingIdempotencyStore(InMemoryIdempotencyStore):
    """An ``IdempotencyStorePort`` that fails at one named operation.

    The idempotency contract has three distinct failure windows - claiming the key, doing the
    work, and recording the outcome - and they have *different* correct answers. A store that
    fails while claiming is safe and must abort before the side effect. A store that fails
    while recording has already allowed the side effect, so the only safe answers are "do not
    repeat the work" and "let the caller see the failure". Collapsing the two windows into one
    blanket "the store is down" test would prove neither.

    Failing a *named* operation rather than all of them is what makes those windows separable.
    """

    def __init__(self, *, fail_on: str, failure: BaseException | None = None) -> None:
        """Create the store with the fault armed.

        Args:
            fail_on: Store method to fail - ``"begin"``, ``"complete"``, ``"release"`` or
                ``"get"``.
            failure: Exception to raise. Defaults to a ``DependencyUnavailableError``.

        """
        super().__init__()
        self.fail_on = fail_on
        self.failure = failure or DependencyUnavailableError(
            f"simulated idempotency store outage during {fail_on}", dependency="postgres"
        )
        #: How many times the named operation was actually invoked, healthy or not. Lets a
        #: test prove the fault was reached rather than routed around.
        self.calls: list[str] = []

    def _maybe_fail(self, operation: str) -> None:
        """Record the call and raise if it is the armed operation.

        Args:
            operation: Name of the store method being entered.

        Raises:
            BaseException: The configured failure, when ``operation`` is armed.

        """
        self.calls.append(operation)
        if self.fail_on == operation:
            raise self.failure

    async def get(
        self, *, tenant_id: str, scope: IdempotencyScope, key: str
    ) -> IdempotencyRecord | None:
        """Look up a key, or fail if armed.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        Returns:
            The record or ``None``.

        """
        self._maybe_fail("get")
        return await super().get(tenant_id=tenant_id, scope=scope, key=key)

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
        """Claim a key, or fail if armed.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            request_fingerprint: Fingerprint of the request body.
            now: Current time.
            retention_seconds: Retention window.

        Returns:
            ``None`` when the key was claimed, else the existing record.

        """
        self._maybe_fail("begin")
        return await super().begin(
            tenant_id=tenant_id,
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            now=now,
            retention_seconds=retention_seconds,
        )

    async def complete(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        response_payload: Mapping[str, Any],
        now: dt.datetime,
    ) -> None:
        """Record the outcome, or fail if armed.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            response_payload: Stored response.
            now: Current time.

        """
        self._maybe_fail("complete")
        await super().complete(
            tenant_id=tenant_id,
            scope=scope,
            key=key,
            response_payload=response_payload,
            now=now,
        )

    async def release(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Release a key, or fail if armed.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        """
        self._maybe_fail("release")
        await super().release(tenant_id=tenant_id, scope=scope, key=key)

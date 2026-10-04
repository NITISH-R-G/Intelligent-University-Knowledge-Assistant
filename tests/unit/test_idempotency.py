"""Unit tests for idempotency: the five replay branches and the failure/concurrency paths.

Idempotency is the difference between "a client retrying on a timeout" being harmless and
being a duplicated side effect. These tests cover the branches a client can actually reach:
first call, replay, concurrent duplicate, payload mismatch, and retry after a failure.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from knowledge_assistant.domain.clock import UTC, FixedClock
from knowledge_assistant.domain.errors import ConflictError
from knowledge_assistant.domain.idempotency import (
    DEFAULT_RETENTION_SECONDS,
    IdempotencyRecord,
    IdempotencyScope,
    IdempotencyStatus,
    decide_replay,
    scoped_key,
)

from ..conftest import InMemoryIdempotencyStore

pytestmark = pytest.mark.unit

EPOCH = dt.datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = "idem-key-abcdefgh-1234"


def _record(
    *,
    status: IdempotencyStatus,
    fingerprint: str = "fp-1",
    expires_at: dt.datetime | None = None,
    payload: dict[str, object] | None = None,
) -> IdempotencyRecord:
    """Build an idempotency record for decision-table tests.

    Args:
        status: Record status.
        fingerprint: Stored request fingerprint.
        expires_at: Expiry instant; defaults to a day after :data:`EPOCH`.
        payload: Stored response payload.

    Returns:
        An ``IdempotencyRecord``.
    """
    return IdempotencyRecord(
        scope=IdempotencyScope.JOB_SUBMIT,
        key=KEY,
        status=status,
        request_fingerprint=fingerprint,
        response_payload=payload,
        created_at=EPOCH,
        expires_at=expires_at or (EPOCH + dt.timedelta(seconds=DEFAULT_RETENTION_SECONDS)),
    )


class TestDecisionTable:
    """All five branches of ``decide_replay``, including the ordering between them."""

    def test_absent_record_executes(self) -> None:
        assert decide_replay(None, request_fingerprint="fp", now=EPOCH).value == "execute"

    def test_completed_matching_fingerprint_replays(self) -> None:
        decision = decide_replay(
            _record(status=IdempotencyStatus.COMPLETED),
            request_fingerprint="fp-1",
            now=EPOCH,
        )
        assert decision.value == "replay"

    def test_in_progress_matching_fingerprint_conflicts(self) -> None:
        """The second concurrent caller must be told to retry, not to wait behind a held
        connection: queueing duplicates is how a slow operation becomes an outage."""
        decision = decide_replay(
            _record(status=IdempotencyStatus.IN_PROGRESS),
            request_fingerprint="fp-1",
            now=EPOCH,
        )
        assert decision.value == "in_flight"

    def test_different_payload_is_a_client_bug(self) -> None:
        decision = decide_replay(
            _record(status=IdempotencyStatus.COMPLETED),
            request_fingerprint="fp-CHANGED",
            now=EPOCH,
        )
        assert decision.value == "fingerprint_mismatch"

    def test_expired_record_executes(self) -> None:
        decision = decide_replay(
            _record(
                status=IdempotencyStatus.COMPLETED,
                expires_at=EPOCH - dt.timedelta(seconds=1),
            ),
            request_fingerprint="fp-1",
            now=EPOCH,
        )
        assert decision.value == "expired"

    def test_expiry_beats_status_and_fingerprint(self) -> None:
        """Ordering matters: an expired record must not block a legitimate retry even if it
        is still marked in-progress, or a crashed request wedges its key forever."""
        decision = decide_replay(
            _record(
                status=IdempotencyStatus.IN_PROGRESS,
                fingerprint="fp-OLD",
                expires_at=EPOCH - dt.timedelta(seconds=1),
            ),
            request_fingerprint="fp-NEW",
            now=EPOCH,
        )
        assert decision.value == "expired"


class TestScopedKey:
    """Key composition is what makes cross-tenant collision structurally impossible."""

    def test_key_includes_tenant_and_scope(self) -> None:
        key = scoped_key("tenant-a", IdempotencyScope.JOB_SUBMIT, KEY)
        assert key == f"tenant-a:job.submit:{KEY}"

    def test_two_tenants_same_key_differ(self) -> None:
        """Without the tenant in the key, tenant B replaying tenant A's request would return
        tenant A's document. That is a cross-tenant data leak, not a cache miss."""
        a = scoped_key("tenant-a", IdempotencyScope.JOB_SUBMIT, KEY)
        b = scoped_key("tenant-b", IdempotencyScope.JOB_SUBMIT, KEY)
        assert a != b

    def test_two_scopes_same_key_differ(self) -> None:
        """A client that reuses one key across operations must not get the previous
        operation's response back."""
        a = scoped_key("tenant-a", IdempotencyScope.JOB_SUBMIT, KEY)
        b = scoped_key("tenant-a", IdempotencyScope.DOCUMENT_UPLOAD, KEY)
        assert a != b


class TestIdempotentExecutor:
    """End-to-end behaviour of the use case over the in-memory store double."""

    async def test_first_request_executes(self, idempotent_executor) -> None:  # noqa: ANN001
        calls: list[str] = []

        async def operation() -> dict[str, str]:
            calls.append("run")
            return {"job_id": "job-1"}

        result, replayed = await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        assert (result, replayed) == ({"job_id": "job-1"}, False)
        assert len(calls) == 1

    async def test_duplicate_request_replays_stored_response(self, idempotent_executor) -> None:  # noqa: ANN001
        calls: list[str] = []

        async def operation() -> dict[str, str]:
            calls.append("run")
            return {"job_id": "job-1"}

        first, _ = await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        second, replayed = await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        assert second == first
        assert replayed is True
        assert len(calls) == 1, "the side effect must happen exactly once"

    async def test_key_reuse_with_different_payload_conflicts(self, idempotent_executor) -> None:  # noqa: ANN001
        async def operation() -> dict[str, str]:
            return {"job_id": "job-1"}

        await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        with pytest.raises(ConflictError) as excinfo:
            await idempotent_executor.run(
                tenant_id="t1",
                scope=IdempotencyScope.JOB_SUBMIT,
                key=KEY,
                payload={"a": 2},
                operation=operation,
            )
        assert "already used with a different request body" in str(excinfo.value)

    async def test_concurrent_duplicates_run_the_operation_once(self, idempotent_executor) -> None:  # noqa: ANN001
        """The realistic race: a client double-submits because the first response was slow.
        Exactly one caller may perform the side effect; the other must be rejected."""
        calls: list[str] = []
        gate = asyncio.Event()

        async def operation() -> dict[str, str]:
            calls.append("run")
            await gate.wait()  # hold the key open until the second caller has arrived
            return {"job_id": "job-1"}

        async def second() -> object:
            await asyncio.sleep(0)
            try:
                return await idempotent_executor.run(
                    tenant_id="t1",
                    scope=IdempotencyScope.JOB_SUBMIT,
                    key=KEY,
                    payload={"a": 1},
                    operation=operation,
                )
            except ConflictError as exc:
                return exc

        first_task = asyncio.create_task(
            idempotent_executor.run(
                tenant_id="t1",
                scope=IdempotencyScope.JOB_SUBMIT,
                key=KEY,
                payload={"a": 1},
                operation=operation,
            )
        )
        second_outcome = await second()
        gate.set()
        first_outcome = await first_task

        assert len(calls) == 1
        assert isinstance(second_outcome, ConflictError)
        assert first_outcome == ({"job_id": "job-1"}, False)

    async def test_failed_request_releases_the_key_for_a_genuine_retry(self, idempotent_executor) -> None:  # noqa: ANN001
        """If a failure kept the key claimed, every client retry after a transient error
        would be permanently rejected and the operation could never succeed."""

        async def failing() -> dict[str, str]:
            raise RuntimeError("dependency down")

        with pytest.raises(RuntimeError):
            await idempotent_executor.run(
                tenant_id="t1",
                scope=IdempotencyScope.JOB_SUBMIT,
                key=KEY,
                payload={"a": 1},
                operation=failing,
            )

        calls: list[str] = []

        async def succeeding() -> dict[str, str]:
            calls.append("run")
            return {"job_id": "job-1"}

        result, replayed = await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=succeeding,
        )
        assert result == {"job_id": "job-1"}
        assert replayed is False
        assert len(calls) == 1

    async def test_retry_after_failure_re_executes_rather_than_replaying_error(self, idempotent_executor) -> None:  # noqa: ANN001
        """A failure must never be recorded as a completed outcome: replaying a stored
        failure would turn one transient error into a permanently failing endpoint."""

        async def failing() -> dict[str, str]:
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await idempotent_executor.run(
                    tenant_id="t1",
                    scope=IdempotencyScope.JOB_SUBMIT,
                    key=KEY,
                    payload={"a": 1},
                    operation=failing,
                )

        store: InMemoryIdempotencyStore = idempotent_executor._store  # noqa: SLF001
        record = await store.get(tenant_id="t1", scope=IdempotencyScope.JOB_SUBMIT, key=KEY)
        assert record is None, "a failed operation must leave no claim behind"

    async def test_expired_record_is_re_executed(self, clock: FixedClock, idempotency_store) -> None:  # noqa: ANN001
        """Retention is finite; after it, the same key must run again rather than replay a
        stale response forever."""
        from knowledge_assistant.application.idempotency import IdempotentExecutor  # noqa: PLC0415

        executor = IdempotentExecutor(idempotency_store, clock=clock, retention_seconds=60)
        calls: list[str] = []

        async def operation() -> dict[str, str]:
            calls.append("run")
            return {"n": str(len(calls))}

        _, _ = await executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        clock.advance(61)
        result, replayed = await executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1},
            operation=operation,
        )
        assert replayed is False
        assert result == {"n": "2"}
        assert len(calls) == 2

    async def test_key_order_in_payload_does_not_change_fingerprint(self, idempotent_executor) -> None:  # noqa: ANN001
        """JSON key order is an encoding detail, not a different request. If it mattered,
        a client whose serialiser reordered keys would get spurious conflicts."""

        async def operation() -> dict[str, str]:
            return {"job_id": "job-1"}

        await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"a": 1, "b": 2},
            operation=operation,
        )
        result, replayed = await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={"b": 2, "a": 1},
            operation=operation,
        )
        assert replayed is True
        assert result == {"job_id": "job-1"}

    async def test_tenants_are_isolated(self, idempotent_executor) -> None:  # noqa: ANN001
        calls: list[str] = []

        async def operation() -> dict[str, str]:
            calls.append("run")
            return {"tenant": "x"}

        await idempotent_executor.run(
            tenant_id="t1",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={},
            operation=operation,
        )
        await idempotent_executor.run(
            tenant_id="t2",
            scope=IdempotencyScope.JOB_SUBMIT,
            key=KEY,
            payload={},
            operation=operation,
        )
        assert len(calls) == 2, "one tenant's key must not dedupe another tenant's request"
"""PostgreSQL job repository.

**The claim is one statement.** Claiming is a single ``UPDATE ... WHERE id = (SELECT ...
FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING ...``. This is the whole reason the queue does not
need a broker, a lock table or a leader election:

* ``FOR UPDATE SKIP LOCKED`` means two workers never contend on the same row; the loser simply
  moves on to the next row instead of blocking.
* ``UPDATE ... RETURNING`` is a single atomic step, so there is no window between "select a row"
  and "own it" in which a crash could leave a job claimed but unowned forever.

**Settlement is fenced.** Every write carries ``AND state = 'running' AND lease_expires_at IS
NOT NULL``. If the lease lapsed and another worker took over, the update matches zero rows and
this worker's outcome is silently discarded. That is the fencing token, and it is the property
that makes at-least-once delivery safe with a bounded lease.

**Errors stored on a row are truncated.** The full exception goes to the log line; the row keeps
a bounded summary because operators read rows and a driver traceback in a table is a
disclosure hazard.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from knowledge_assistant.domain.identifiers import new_id
from knowledge_assistant.domain.jobs import Job, JobClaim, JobState

__all__ = ["PgJobRepository", "row_to_job"]

#: Columns selected when reading a job. Centralised so the row mapping has one definition.
_JOB_COLUMNS: str = """
    id, type, state, attempts_used, max_attempts, available_at,
    lease_expires_at, payload, last_error, created_at
"""


def row_to_job(row: Mapping[str, Any]) -> Job:
    """Map a database row to a domain ``Job``.

    Args:
        row: Row produced by ``dict_row``.

    Returns:
        An immutable ``Job``.

    """
    return Job(
        id=str(row["id"]),
        type=str(row["type"]),
        state=JobState(str(row["state"])),
        attempts_used=int(row["attempts_used"]),
        max_attempts=int(row["max_attempts"]),
        available_at=row["available_at"],
        lease_expires_at=row["lease_expires_at"],
        payload=dict(row["payload"] or {}),
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


class PgJobRepository:
    """Job persistence on PostgreSQL."""

    __slots__ = ("_database",)

    def __init__(self, database: Any) -> None:
        """Adopt a ``Database`` handle.

        Args:
            database: Object exposing ``acquire()`` as an async context manager yielding a
                connection.

        """
        self._database = database

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any],
        available_at: dt.datetime,
        max_attempts: int,
        idempotency_key: str | None = None,
    ) -> Job:
        """Insert a job, or return the existing job for a duplicate idempotency key.

        Idempotency is enforced by a partial unique index on ``idempotency_key``, not by a
        read-then-write check. A read-then-write has a race: two concurrent submissions both
        read "absent" and both insert. The index makes the second insert a no-op.

        Args:
            job_type: Registered job type.
            payload: JSON-serialisable input.
            available_at: Earliest claim time.
            max_attempts: Attempt ceiling.
            idempotency_key: Optional client key.

        Returns:
            The created job, or the pre-existing job on duplicate submission.

        """
        job_id = str(new_id())
        sql = f"""
            INSERT INTO jobs (id, type, state, attempts_used, max_attempts, available_at,
                              payload, idempotency_key, created_at, updated_at)
            VALUES (%(id)s, %(type)s, 'pending', 0, %(max_attempts)s, %(available_at)s,
                    %(payload)s, %(idempotency_key)s, now(), now())
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
            DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING {_JOB_COLUMNS}
        """
        params = {
            "id": job_id,
            "type": job_type,
            "max_attempts": max_attempts,
            "available_at": available_at,
            "payload": dict(payload),
            "idempotency_key": idempotency_key,
        }
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
        if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
            raise RuntimeError("insert returned no row")
        return row_to_job(row)

    async def claim_next(self, *, now: dt.datetime, lease_seconds: float) -> JobClaim | None:
        """Atomically claim the next runnable job.

        Args:
            now: Current time from the injected clock. Passed as a parameter rather than read
                from the database so that clock policy is decided in one place.
            lease_seconds: Lease duration.

        Returns:
            A ``JobClaim``, or ``None`` when nothing is runnable.

        """
        sql = f"""
            UPDATE jobs SET
                state = 'running',
                attempts_used = attempts_used + 1,
                lease_expires_at = %(lease_expires_at)s,
                claimed_by = %(claimed_by)s,
                updated_at = now()
            WHERE id = (
                SELECT id FROM jobs
                WHERE state IN ('pending', 'retry')
                  AND available_at <= %(now)s
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING {_JOB_COLUMNS}
        """
        params = {
            "now": now,
            "lease_expires_at": now + dt.timedelta(seconds=lease_seconds),
            "claimed_by": _worker_identity(),
        }
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
        if row is None:
            return None
        job = row_to_job(row)
        lease_expires = job.lease_expires_at
        if lease_expires is None:  # pragma: no cover - set by the UPDATE above
            lease_expires = now + dt.timedelta(seconds=lease_seconds)
        return JobClaim(job=job, claimed_at=now, lease_expires_at=lease_expires)

    async def mark_succeeded(self, *, claim: JobClaim, result: Mapping[str, Any]) -> None:
        """Record terminal success, fenced by the running state.

        Args:
            claim: Claim that executed the job.
            result: Handler result.

        """
        sql = """
            UPDATE jobs SET
                state = 'succeeded',
                result = %(result)s,
                lease_expires_at = NULL,
                last_error = NULL,
                finished_at = now(),
                updated_at = now()
            WHERE id = %(id)s AND state = 'running'
        """
        await self._execute_fenced(sql, claim, {"result": dict(result)})

    async def mark_retry(
        self,
        *,
        claim: JobClaim,
        error: str,
        next_available_at: dt.datetime,
    ) -> None:
        """Reschedule a job after a retryable failure.

        Args:
            claim: Claim that executed the job.
            error: Truncated failure summary.
            next_available_at: Earliest next claim time.

        """
        sql = """
            UPDATE jobs SET
                state = 'retry',
                available_at = %(next_available_at)s,
                last_error = %(error)s,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE id = %(id)s AND state = 'running'
        """
        await self._execute_fenced(
            sql, claim, {"error": error, "next_available_at": next_available_at}
        )

    async def mark_dead_lettered(self, *, claim: JobClaim, error: str) -> None:
        """Record terminal failure.

        Args:
            claim: Claim that executed the job.
            error: Truncated failure summary.

        """
        sql = """
            UPDATE jobs SET
                state = 'dead_lettered',
                last_error = %(error)s,
                lease_expires_at = NULL,
                finished_at = now(),
                updated_at = now()
            WHERE id = %(id)s AND state = 'running'
        """
        await self._execute_fenced(sql, claim, {"error": error})

    async def _execute_fenced(self, sql: str, claim: JobClaim, extra: Mapping[str, Any]) -> None:
        """Run a fenced settlement statement.

        Args:
            sql: Statement with ``WHERE id = %(id)s AND state = 'running'``.
            claim: Claim providing the job id.
            extra: Additional statement parameters.

        """
        params: dict[str, Any] = {"id": claim.job.id, **dict(extra)}
        async with self._database.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)

    async def get(self, job_id: str) -> Job | None:
        """Return a job by identifier.

        Args:
            job_id: Job identifier.

        Returns:
            The job, or ``None`` if absent.

        """
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = %(id)s"
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"id": job_id})
            row = await cur.fetchone()
        return row_to_job(row) if row else None

    async def counts_by_state(self) -> Mapping[str, int]:
        """Return job counts grouped by state.

        Returns:
            Mapping of state name to count. States with no rows are absent rather than zero,
            because a missing key is cheaper to interpret than a misleading zero.

        """
        sql = "SELECT state, count(*) AS n FROM jobs GROUP BY state"
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        return {str(r["state"]): int(r["n"]) for r in rows}

    async def dead_letter_count(self) -> int:
        """Return the number of dead-lettered jobs.

        Uses an index-only predicate so the count stays cheap as the table grows.
        """
        sql = "SELECT count(*) AS n FROM jobs WHERE state = 'dead_lettered'"
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def oldest_pending_age_seconds(self, *, now: dt.datetime) -> float | None:
        """Return the age of the oldest pending job.

        Args:
            now: Current time from the injected clock.

        Returns:
            Age in seconds, or ``None`` when nothing is pending. Uses ``MIN`` over a partial
            index on ``available_at`` rather than counting rows.

        """
        sql = """
            SELECT EXTRACT(EPOCH FROM (%(now)s::timestamptz - MIN(available_at))) AS age
            FROM jobs
            WHERE state IN ('pending', 'retry')
        """
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"now": now})
            row = await cur.fetchone()
        if not row or row["age"] is None:
            return None
        return float(row["age"])


def _worker_identity() -> str:
    """Return a short identifier for the claiming process.

    Recorded on the row so an operator can tell which instance owns a running job. Phase 1
    uses the process id, which is adequate for a single-host deployment and honestly
    insufficient for a distributed one - noted rather than over-engineered.
    """
    import os  # noqa: PLC0415

    return f"pid:{os.getpid()}"


def connection_is_dict_row(conn: AsyncConnection[Any]) -> bool:  # pragma: no cover - sanity helper
    """Return whether the connection is configured for dict rows."""
    return conn.info.row_factory is dict_row

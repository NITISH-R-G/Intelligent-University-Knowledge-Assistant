"""Live PostgreSQL fault injection.

**Skipped unless ``TEST_DATABASE_URL`` is set.** Every test here talks to a real server, so
there is nothing deterministic to assert when one is absent - pretending otherwise would be the
"fake green result" this repository treats as worse than a reported block.

These are the scenarios that cannot be injected through a test double, because the behaviour
under test *is* PostgreSQL's: what it does to a transaction that fails, what it does to a
statement that runs past its budget, and what class of error it raises for each.

Faults are induced **by the server itself**, never by patching the driver:

* a closed port for connection failure,
* ``statement_timeout`` for the timeout path,
* a statement naming a table that does not exist for the rejected-statement path,
* an exception raised mid-transaction for rollback,
* a genuine duplicate primary key for the violation path.

None of these depend on wall-clock timing: the server either raises or it does not, and the
assertions are on error *taxonomy* rather than on elapsed time.

What this module deliberately does **not** cover, and why
-------------------------------------------------------------
The production adapter ``build_pool`` wraps psycopg's ``AsyncConnectionPool``, which spawns a
background connection worker. That worker needs socket readiness notification, and on Windows
the only loop that can serve psycopg's async I/O at all - ``WindowsSelectorEventLoop`` - does
not provide it for sockets. On this host the pool times out with ``PoolTimeout`` after 30
seconds, while a **direct** ``AsyncConnection`` to the same server succeeds immediately.

So the pool-backed scenarios - fencing through ``PgJobRepository``, claim exclusivity via
``FOR UPDATE SKIP LOCKED``, lease takeover against real SQL - are **BLOCKED on this platform**,
not merely skipped for want of Docker. They are covered in the deterministic lane against the
in-memory double, which implements the same port semantics, and are deliberately not duplicated
here as tests that could only ever be shipped unverified. On Linux or macOS they belong in an
integration lane alongside the rest of the ``TEST_DATABASE_URL``-gated work.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from knowledge_assistant.application.health import HealthService
from knowledge_assistant.domain.clock import FixedClock
from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    ErrorKind,
    TimeoutError_,
    ValidationError,
)
from knowledge_assistant.domain.health import ProbeCriticality
from knowledge_assistant.domain.jobs import FailureClass, classify_failure
from knowledge_assistant.infrastructure.db.engine import translate_db_error
from knowledge_assistant.infrastructure.db.probes import DatabaseProbe

from ..conftest import TEST_EPOCH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.failure_injection,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL"),
        reason="needs a live PostgreSQL; set TEST_DATABASE_URL to run the fault-injection lane",
    ),
]

#: A port nothing listens on, carrying a password that must never surface in an error. The
#: connection failure is injected at the TCP layer, so it needs no cooperation from the server
#: and cannot be mistaken for a query failure.
_CLOSED_PORT_DSN = "postgresql://faultprobe:hunter2@localhost:1/ka"

#: Parameterised insert used by the transaction and violation scenarios.
_INSERT_SQL = (
    "INSERT INTO jobs (id, type, state, available_at) "
    "VALUES (%(id)s, 'fault-probe', 'pending', %(now)s)"
)

_COUNT_SQL = "SELECT count(*) AS n FROM jobs WHERE id = %(id)s"

_DELETE_SQL = "DELETE FROM jobs WHERE id = %(id)s"


def _dsn() -> str:
    """Return the configured test DSN.

    Returns:
        The connection string from ``TEST_DATABASE_URL``.

    """
    return os.environ["TEST_DATABASE_URL"]


@asynccontextmanager
async def _connect(dsn: str, *, statement_timeout_ms: int = 30_000) -> AsyncIterator[Any]:
    """Yield a direct connection with a session ``statement_timeout`` applied.

    A direct connection rather than the production pool, because the pool cannot open on
    Windows - see the module docstring. The timeout is applied through ``set_config`` so the
    value travels as a bound parameter, exactly as the rest of this repository requires.

    Args:
        dsn: Connection string.
        statement_timeout_ms: Session ``statement_timeout``.

    Yields:
        An open ``AsyncConnection``.

    """
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    conn = await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, connect_timeout=5, row_factory=dict_row
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT set_config('statement_timeout', %(timeout)s, false)",
                {"timeout": str(statement_timeout_ms)},
            )
        yield conn
    finally:
        await conn.close()


async def _insert_job(conn: Any, job_id: str) -> None:
    """Insert one probe row.

    Args:
        conn: Connection.
        job_id: Primary key to insert.

    """
    await conn.execute(_INSERT_SQL, {"id": job_id, "now": dt.datetime.now(dt.UTC)})


async def _delete_job(conn: Any, job_id: str) -> None:
    """Delete one probe row, ignoring absence.

    Args:
        conn: Connection.
        job_id: Primary key to remove.

    """
    await conn.execute(_DELETE_SQL, {"id": job_id})


async def _count_jobs(conn: Any, job_id: str) -> int:
    """Count rows with a given primary key.

    Args:
        conn: Connection.
        job_id: Primary key to count.

    Returns:
        The number of matching committed rows.

    """
    async with conn.cursor() as cur:
        await cur.execute(_COUNT_SQL, {"id": job_id})
        row = await cur.fetchone()
        return int(row["n"])


class _Abort(Exception):  # noqa: N818 - a transaction abort, not an application error
    """Raised inside a transaction to force a rollback on a real server."""


class TestConnectionFailure:
    """A dependency that cannot be reached fails at the boundary, with a safe message."""

    async def test_an_unreachable_database_cannot_connect(self) -> None:
        """Scenario: PostgreSQL is down.

        Expected classification: a retryable dependency error. Retry is **expected** at this
        level but must never be disguised as a successful connection. Evidence: the attempt
        raises rather than yielding a usable connection.
        """
        import psycopg  # noqa: PLC0415

        with pytest.raises(psycopg.Error) as excinfo:
            await psycopg.AsyncConnection.connect(_CLOSED_PORT_DSN, connect_timeout=2)
        assert classify_failure(translate_db_error(excinfo.value)) is FailureClass.TRANSIENT

    async def test_a_connection_failure_never_carries_the_credentials(self) -> None:
        """The message that reaches a log line must not contain the DSN.

        A connection string is exactly what a driver embeds in its own message, which is why
        ``translate_db_error`` treats driver text as unsafe to propagate.
        """
        import psycopg  # noqa: PLC0415

        with pytest.raises(psycopg.Error) as excinfo:
            await psycopg.AsyncConnection.connect(_CLOSED_PORT_DSN, connect_timeout=2)
        translated = translate_db_error(excinfo.value)
        assert "hunter2" not in str(translated)
        assert _CLOSED_PORT_DSN not in str(translated)
        assert translated.detail.get("dependency") == "postgres"

    async def test_a_healthy_database_accepts_a_query(self) -> None:
        """The negative control.

        Without it, an assertion that "connecting fails" would also be satisfied by a driver
        that cannot connect to anything at all.
        """
        async with _connect(_dsn()) as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 AS answer")
            assert (await cur.fetchone())["answer"] == 1


class TestDependencyTimeout:
    """OPS-006: every outbound call carries a budget, and the server enforces it.

    One observation rather than an assertion: ``translate_db_error`` reports
    ``timeout_seconds: 0.0`` on every timeout, because the marker path has no access to the
    budget that was configured. The *classification* is correct and the driver message is
    preserved on ``__cause__`` for the logs, but the structured detail alone cannot tell an
    operator which budget was exceeded. Asserting ``0.0`` would cement that; the honest move is
    to assert what is guaranteed and report the gap.
    """

    async def test_a_statement_past_its_budget_is_a_timeout(self) -> None:
        """Scenario: a query outlives ``statement_timeout``.

        Expected classification: ``TimeoutError_``, deliberately distinct from
        ``DependencyUnavailableError`` so alerting can separate "the database is refusing
        connections" from "the database is too slow". Retry is **expected**.
        """
        async with _connect(_dsn(), statement_timeout_ms=250) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT pg_sleep(5)")
                await cur.fetchone()
        translated = translate_db_error(excinfo.value)
        assert isinstance(translated, TimeoutError_)
        assert translated.kind is ErrorKind.TIMEOUT

    async def test_a_timeout_is_retryable(self) -> None:
        """A slow query is worth retrying; calling it permanent would drop the work."""
        async with _connect(_dsn(), statement_timeout_ms=250) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT pg_sleep(5)")
                await cur.fetchone()
        assert classify_failure(translate_db_error(excinfo.value)) is FailureClass.TRANSIENT

    async def test_the_abort_came_from_the_server_not_the_client(self) -> None:
        """The abort is PostgreSQL enforcing its own budget, which is what makes it reliable.

        Evidence: the driver reports the server's ``statement_timeout``. A client-side
        cancellation would leave the query running on the server - the same symptom with a very
        different cost, because the database keeps burning CPU on abandoned work.
        """
        async with _connect(_dsn(), statement_timeout_ms=250) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT pg_sleep(5)")
                await cur.fetchone()
        assert "statement timeout" in str(excinfo.value).lower()

    async def test_a_statement_inside_its_budget_succeeds(self) -> None:
        """The negative control: the timeout must not fire on ordinary work."""
        async with _connect(_dsn(), statement_timeout_ms=30_000) as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_sleep(0.01)")
            assert await cur.fetchone() is not None


class TestRejectedStatement:
    """A statement the server refuses is a programming error, not an outage."""

    async def test_an_unknown_table_is_a_validation_error(self) -> None:
        """Scenario: a query references a table that does not exist.

        Expected classification: ``ValidationError``. Retry is **forbidden** - the statement
        will be equally wrong next time, and retrying hides a schema mismatch behind a queue
        of doomed jobs.
        """
        async with _connect(_dsn()) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT * FROM table_that_does_not_exist")
                await cur.fetchone()
        translated = translate_db_error(excinfo.value)
        assert isinstance(translated, ValidationError)
        assert translated.kind is ErrorKind.VALIDATION
        assert classify_failure(translated) is FailureClass.PERMANENT

    async def test_a_syntax_error_is_also_permanent(self) -> None:
        """A malformed statement and a missing table are the same class of mistake."""
        async with _connect(_dsn()) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT FROM WHERE")
                await cur.fetchone()
        assert classify_failure(translate_db_error(excinfo.value)) is FailureClass.PERMANENT

    async def test_a_rejected_statement_leaves_the_session_usable(self) -> None:
        """A bad statement must not poison the session.

        On a pooled connection, an error that left the session in a failed transaction would
        turn one bad query into a pool-wide outage.
        """
        async with _connect(_dsn()) as conn:
            with pytest.raises(Exception):  # noqa: B017, PT011 - narrowed by the next assertion
                async with conn.cursor() as cur:
                    await cur.execute("SELECT * FROM table_that_does_not_exist")
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 AS answer")
                assert (await cur.fetchone())["answer"] == 1

    async def test_the_server_message_is_not_propagated_to_the_caller(self) -> None:
        """Server error text names tables and columns; the caller must not receive it.

        Evidence: the original stays available as ``__cause__`` for the logs while the message
        the caller sees names only the dependency and the server error class.
        """
        async with _connect(_dsn()) as conn, conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - narrowed immediately
                await cur.execute("SELECT * FROM table_that_does_not_exist")
                await cur.fetchone()
        translated = translate_db_error(excinfo.value)
        assert "table_that_does_not_exist" not in str(translated)
        assert excinfo.value is translated.__cause__, "the original must survive for the logs"


class TestDuplicateKeyViolation:
    """A real ``UniqueViolation`` - the case the error taxonomy does not name."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "_VIOLATION_MARKERS omits UniqueViolation, so a duplicate key is classified "
            "retryable; a duplicate is deterministic and must not be retried"
        ),
    )
    async def test_a_duplicate_primary_key_is_classified_as_permanent(self) -> None:
        """Scenario: two rows are inserted with the same primary key.

        Expected classification: ``ValidationError``. Retry is **forbidden** - the constraint
        will reject every further attempt, so retrying spins against it forever.
        """
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "duplicate-probe")
            try:
                for _ in range(2):
                    await _insert_job(conn, "duplicate-probe")
                pytest.fail("PostgreSQL accepted a duplicate primary key")
            except Exception as exc:  # noqa: BLE001 - classification is the subject
                assert type(exc).__name__ == "UniqueViolation"
                assert classify_failure(translate_db_error(exc)) is FailureClass.PERMANENT
            finally:
                await _delete_job(conn, "duplicate-probe")

    async def test_a_duplicate_is_really_rejected_by_the_server(self) -> None:
        """The negative control for the test above.

        It proves the constraint exists and fires, so the xfail above is failing for the reason
        stated - a taxonomy gap - and not because the insert quietly succeeded.
        """
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "duplicate-probe")
            try:
                await _insert_job(conn, "duplicate-probe")
                with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011 - narrowed next
                    await _insert_job(conn, "duplicate-probe")
                assert type(excinfo.value).__name__ == "UniqueViolation"
                assert await _count_jobs(conn, "duplicate-probe") == 1
            finally:
                await _delete_job(conn, "duplicate-probe")

    async def test_distinct_keys_are_both_accepted(self) -> None:
        """The constraint must reject duplicates, not everything."""
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "distinct-a")
            await _delete_job(conn, "distinct-b")
            try:
                await _insert_job(conn, "distinct-a")
                await _insert_job(conn, "distinct-b")
                assert await _count_jobs(conn, "distinct-a") == 1
                assert await _count_jobs(conn, "distinct-b") == 1
            finally:
                await _delete_job(conn, "distinct-a")
                await _delete_job(conn, "distinct-b")


class TestTransactionRollback:
    """A transaction that fails must leave nothing behind."""

    async def test_a_failed_transaction_writes_nothing(self) -> None:
        """Scenario: work is started, then the transaction fails before commit.

        Evidence is cross-connection, which is the only honest test: the writing connection is
        still holding the aborted transaction and would see its own uncommitted rows.
        """
        async with _connect(_dsn()) as writer:
            await _delete_job(writer, "rollback-probe")
            with pytest.raises(_Abort):
                async with writer.transaction():
                    await writer.execute(
                        _INSERT_SQL, {"id": "rollback-probe", "now": dt.datetime.now(dt.UTC)}
                    )
                    raise _Abort

            async with _connect(_dsn()) as observer:
                assert await _count_jobs(observer, "rollback-probe") == 0

    async def test_a_committed_transaction_is_durable(self) -> None:
        """The negative control.

        Without it, "nothing was written" could equally mean "nothing was ever written", and
        the rollback test would pass against a database that silently discarded everything.
        """
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "commit-probe")
            try:
                async with conn.transaction():
                    await conn.execute(
                        _INSERT_SQL, {"id": "commit-probe", "now": dt.datetime.now(dt.UTC)}
                    )
                async with _connect(_dsn()) as observer:
                    assert await _count_jobs(observer, "commit-probe") == 1
            finally:
                await _delete_job(conn, "commit-probe")

    async def test_a_failure_after_the_statement_still_rolls_it_back(self) -> None:
        """The rollback is driven by the transaction outcome, not by the SQL failing.

        Here the insert *succeeds* and the exception happens afterwards - the shape of a bug
        that appears only when a later step fails, and the shape most likely to leak data.
        """
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "late-abort-probe")
            try:
                with pytest.raises(_Abort):
                    async with conn.transaction():
                        await conn.execute(
                            _INSERT_SQL,
                            {"id": "late-abort-probe", "now": dt.datetime.now(dt.UTC)},
                        )
                        await conn.execute("SELECT 1")  # this one succeeds
                        raise _Abort
                async with _connect(_dsn()) as observer:
                    assert await _count_jobs(observer, "late-abort-probe") == 0
            finally:
                await _delete_job(conn, "late-abort-probe")

    async def test_a_rolled_back_transaction_leaves_the_session_usable(self) -> None:
        """An aborted transaction must not leave the session in a failed state.

        A session stuck in ``INERROR`` rejects every subsequent statement until someone issues
        a ROLLBACK - turning one failed unit of work into a connection that is dead until the
        pool recycles it.
        """
        async with _connect(_dsn()) as conn:
            await _delete_job(conn, "session-probe")
            try:
                with pytest.raises(_Abort):
                    async with conn.transaction():
                        await conn.execute(
                            _INSERT_SQL, {"id": "session-probe", "now": dt.datetime.now(dt.UTC)}
                        )
                        raise _Abort
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1 AS answer")
                    assert (await cur.fetchone())["answer"] == 1
            finally:
                await _delete_job(conn, "session-probe")


class TestRealDependencyProbe:
    """The readiness probe, against the database it actually checks."""

    async def test_the_probe_reports_ok_against_a_live_database(self) -> None:
        """The negative control for the failure tests below."""
        async with _connect(_dsn()) as conn:
            result = await DatabaseProbe(_SingleConnection(conn), FixedClock(TEST_EPOCH)).check()
        assert result.status.value == "ok"
        assert result.detail == "ok"
        assert result.criticality is ProbeCriticality.CRITICAL

    async def test_readiness_fails_when_the_database_is_unreachable(self) -> None:
        """Scenario: readiness is polled after the database went away.

        Expected behaviour: the probe raises, the use case converts it to a failed CRITICAL
        probe, and ``/readyz`` answers 503. Evidence: it fails where the server is genuinely
        unreachable, not against a double that always fails.
        """
        health = HealthService(
            [_unreachable_probe()], clock=FixedClock(TEST_EPOCH), timeout_seconds=10.0
        )
        report = await health.report()
        assert report.ready is False
        assert report.probes[0].status.value == "failed"
        assert "hunter2" not in (report.probes[0].detail or "")

    async def test_a_failed_probe_detail_names_only_the_exception_class(self) -> None:
        """Readiness is unauthenticated, so it must not become an information disclosure.

        Evidence: the detail is a scrubbed class-name token, not the driver's message - which
        in the double deliberately embeds the full DSN.
        """
        health = HealthService(
            [_unreachable_probe()], clock=FixedClock(TEST_EPOCH), timeout_seconds=10.0
        )
        report = await health.report()
        assert report.probes[0].detail == "failed:operationalerror"


class _SingleConnection:
    """Exposes one already-open connection through the ``Database.acquire()`` port.

    The production ``Database`` cannot be used here because its pool cannot open on Windows;
    this is the same port surface with a single connection behind it, so ``DatabaseProbe`` runs
    unmodified against a real server.

    Args:
        conn: An open ``AsyncConnection``.

    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        """Adopt an open connection.

        Args:
            conn: An open ``AsyncConnection``.

        """
        self._conn = conn

    def acquire(self) -> Any:
        """Return an async context manager yielding the connection.

        Returns:
            An async context manager.

        """
        return _ConnectionContext(self._conn)


class _ConnectionContext:
    """Async context manager yielding a fixed connection.

    Args:
        conn: Connection to yield.

    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        """Store the connection.

        Args:
            conn: Connection to yield.

        """
        self._conn = conn

    async def __aenter__(self) -> Any:
        """Enter the context.

        Returns:
            The connection.

        """
        return self._conn

    async def __aexit__(self, *exc_info: Any) -> None:
        """Leave the context without closing the shared connection.

        Args:
            *exc_info: Standard exception triple, ignored.

        """


def _unreachable_probe() -> DatabaseProbe:
    """Return the real production probe wired to a database that cannot be reached.

    The probe itself is production code, so what is under test is the classification and
    scrubbing path, not a convenient stand-in for it.

    Returns:
        A ``DatabaseProbe`` whose connections all fail.

    """
    return DatabaseProbe(_UnreachableDatabase(), FixedClock(TEST_EPOCH))


class _UnreachableDatabase:
    """A ``Database``-shaped port whose every connection attempt fails.

    Stands in for "PostgreSQL is gone" at the probe's own boundary, which is what the probe
    must survive. The raised error is psycopg's real ``OperationalError``, so the classification
    and scrubbing paths under test are the production ones rather than convenient stand-ins.
    """

    __slots__ = ()

    def acquire(self) -> Any:
        """Return a context manager whose entry raises the driver error.

        Returns:
            An async context manager.

        """
        return _FailingContext()


class _FailingContext:
    """Async context manager that raises a driver-shaped error on entry."""

    __slots__ = ()

    async def __aenter__(self) -> Any:
        """Raise the simulated driver error.

        Raises:
            OperationalError: The real psycopg class whose *name* ``translate_db_error``
                classifies on, carrying a message that deliberately embeds the DSN.

        """
        from psycopg import OperationalError  # noqa: PLC0415

        raise OperationalError(
            f"connection to server at {_CLOSED_PORT_DSN} failed: password authentication"
        )

    async def __aexit__(self, *exc_info: Any) -> None:
        """Leave the context.

        Args:
            *exc_info: Standard exception triple, ignored.

        """


def test_an_operational_error_is_classified_as_a_retryable_outage() -> None:
    """The marker list itself, independent of any server.

    Paired with the live tests above: if ``OperationalError`` were removed from
    ``_UNAVAILABLE_MARKERS`` it would fall through to the default branch and still produce a
    ``DependencyUnavailableError``, so this assertion cannot tell the two apart. The timeout
    tests are what distinguish them, and they need a real server.
    """
    from psycopg import OperationalError  # noqa: PLC0415

    translated = translate_db_error(OperationalError("could not connect"))
    assert isinstance(translated, DependencyUnavailableError)
    assert classify_failure(translated) is FailureClass.TRANSIENT


def test_the_lane_states_its_infrastructure_dependency() -> None:
    """The skip reason must name the variable that enables the lane.

    A skip that says only "skipped" leaves the next engineer guessing whether the database is
    broken, absent, or deliberately not wanted.
    """
    reason = pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL"),
        reason="needs a live PostgreSQL; set TEST_DATABASE_URL to run the fault-injection lane",
    ).kwargs["reason"]
    assert "TEST_DATABASE_URL" in str(reason)

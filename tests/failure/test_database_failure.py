"""Failure injection: the database dependency.

Covers Phase 0 **OPS-006** ("timeouts on every outbound call ... verified by fault
injection") and the error taxonomy in
[`docs/07-api/ERROR_MODEL.md`](../../docs/07-api/ERROR_MODEL.md).

Live PostgreSQL fault injection is **BLOCKED** in this environment: the Docker daemon is
unavailable, so no container can be started, no connection can be severed mid-query, and
no transaction can be observed rolling back. Nothing here claims otherwise.

What *is* proven here is everything on the near side of the driver boundary, which is
where the interesting policy lives:

* driver exceptions are classified into the right ``ErrorKind``
* the driver's own message never reaches a client
* the original exception is preserved for operators
* an unreachable database fails **startup** rather than producing a half-started process

That boundary is the seam the architecture provides (the ``JobRepositoryPort`` and
``Database`` abstractions), so these tests inject through it rather than reaching into
production internals.
"""

from __future__ import annotations

import pytest

from knowledge_assistant.domain.clock import FixedClock
from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    ErrorKind,
    TimeoutError_,
    ValidationError,
)
from knowledge_assistant.domain.health import ProbeCriticality, ProbeStatus
from knowledge_assistant.infrastructure.db.engine import translate_db_error
from knowledge_assistant.infrastructure.db.probes import DatabaseProbe

from ..conftest import TEST_EPOCH, CountingDatabase

pytestmark = pytest.mark.failure_injection


class _DriverError(Exception):
    """Stand-in for a psycopg exception type, named to match the real ones.

    ``translate_db_error`` classifies by *class name*, so a realistic name is what makes
    the mapping test meaningful. A generic ``Exception`` here would match nothing and the
    test would pass for the wrong reason.
    """


class OperationalError(_DriverError):
    """Mirrors ``psycopg.OperationalError``: the server is unreachable."""


class QueryCanceled(_DriverError):  # noqa: N818 - named for the real psycopg class it stands in for
    """Mirrors a statement-timeout abort."""


class ProgrammingError(_DriverError):
    """Mirrors ``psycopg.ProgrammingError``: the statement itself is wrong."""


class TestDriverErrorTranslation:
    """OPS-006: an outbound call failure becomes a classified, retryable error."""

    def test_unreachable_server_is_dependency_unavailable(self) -> None:
        """Scenario: PostgreSQL is down. A connection failure is retryable.

        Classifying this as permanent would dead-letter in-flight work during a database
        blip; classifying it as a client error would be nonsense. It must be
        ``DEPENDENCY_UNAVAILABLE``.
        """
        translated = translate_db_error(OperationalError("connection refused"))
        assert isinstance(translated, DependencyUnavailableError)
        assert translated.kind is ErrorKind.DEPENDENCY_UNAVAILABLE

    def test_timeout_is_a_timeout(self) -> None:
        """Scenario: a statement exceeds its budget.

        This is the case OPS-006 exists for: without a timeout a bad query becomes a
        pool-exhaustion outage, so the classification must be distinct from a plain
        unavailability, even though both are retryable.
        """
        translated = translate_db_error(QueryCanceled("canceling statement due to timeout"))
        assert isinstance(translated, TimeoutError_)
        assert translated.kind is ErrorKind.TIMEOUT

    def test_rejected_statement_is_a_validation_failure(self) -> None:
        """Scenario: the database rejects the statement as malformed.

        This is the *caller's* fault, not the database's: retrying it unchanged will fail
        identically every time. Marking it retryable would spin a worker against a
        statement that will never be accepted.
        """
        translated = translate_db_error(ProgrammingError('relation "jobs" does not exist'))
        assert isinstance(translated, ValidationError)
        assert translated.kind is ErrorKind.VALIDATION

    def test_unrecognised_driver_error_fails_closed_as_unavailable(self) -> None:
        """An unrecognised error is treated as retryable-unavailable, never as success.

        Failing toward "unavailable" costs a bounded number of retries. Failing the other
        way - treating an unknown database error as a permanent client fault - would
        discard real work.
        """
        translated = translate_db_error(RuntimeError("something entirely new"))
        assert isinstance(translated, DependencyUnavailableError)

    def test_driver_message_never_reaches_the_client(self) -> None:
        """The security property: psycopg messages can embed the DSN and SQL text.

        A connection error from psycopg routinely contains
        ``connection to server at "db.internal" (10.0.0.5), port 5432 failed`` and
        sometimes a fragment of the connection string. Echoing that to an unauthenticated
        caller discloses internal topology.
        """
        secret = "password=hunter2 host=10.0.0.5 dbname=ka"
        translated = translate_db_error(OperationalError(f"connection failed: {secret}"))
        rendered = f"{translated!r} {getattr(translated, 'detail', '')}"
        assert "hunter2" not in rendered
        assert "10.0.0.5" not in rendered

    def test_original_exception_is_preserved_for_operators(self) -> None:
        """The client is protected *and* the operator keeps the detail.

        Suppressing the driver exception entirely would make production incidents
        undiagnosable. The protection is that it is attached as ``__cause__`` - visible in
        logs, never rendered into a response - rather than discarded.
        """
        original = OperationalError("connection refused")
        translated = translate_db_error(original)
        assert translated.__cause__ is original

    def test_classification_matches_the_retry_policy(self) -> None:
        """Every translated kind must carry a coherent retryable flag.

        This is the join between the error taxonomy and the retry policy: a kind marked
        non-retryable that the worker treats as retryable would spin forever, and the
        reverse would discard work. Asserting it here means a change to either table
        without the other fails.
        """
        retryable_expectations = {
            OperationalError: True,
            QueryCanceled: True,
            ProgrammingError: False,
        }
        for driver_error, expected in retryable_expectations.items():
            translated = translate_db_error(driver_error("x"))
            assert translated.retryable is expected, f"{driver_error.__name__}: {translated.kind}"


class TestDatabaseProbeFailure:
    """A dead database must be visible on readiness, and must not crash the probe."""

    def _failing_database(self, message: str) -> CountingDatabase:
        """Return a database whose every checkout fails with ``message``.

        Args:
            message: Driver-style message, potentially containing secrets.

        Returns:
            The failing database double.

        """
        return CountingDatabase(failure=OperationalError(message))

    async def test_unreachable_database_reports_the_probe_failed(self) -> None:
        """Scenario: the database is unavailable.

        ``DatabaseProbe`` deliberately lets the driver exception propagate; the use case
        converts it. This asserts the outcome of that conversion is a failed *result*,
        not an unhandled exception that would turn a dependency outage into a 500 on the
        readiness endpoint - the opposite of what readiness is for.
        """
        from knowledge_assistant.application.health import HealthService  # noqa: PLC0415

        probe = DatabaseProbe(self._failing_database("connection refused"), _fixed_clock())
        service = HealthService([probe], clock=_fixed_clock(), timeout_seconds=1.0)

        report = await service.report()

        assert report.ready is False
        assert len(report.probes) == 1
        result = report.probes[0]
        assert result.status is ProbeStatus.FAILED
        assert result.criticality is ProbeCriticality.CRITICAL

    async def test_failed_probe_detail_does_not_disclose_infrastructure(self) -> None:
        """The readiness body is unauthenticated; it must not leak hostnames or DSNs."""
        from knowledge_assistant.application.health import HealthService  # noqa: PLC0415

        probe = DatabaseProbe(
            self._failing_database("could not connect to host=10.0.0.5 password=hunter2"),
            _fixed_clock(),
        )
        service = HealthService([probe], clock=_fixed_clock(), timeout_seconds=1.0)

        report = await service.report()

        detail = report.probes[0].detail or ""
        assert "hunter2" not in detail
        assert "10.0.0.5" not in detail

    async def test_healthy_database_still_reports_ok(self) -> None:
        """The negative control: the probe is not permanently failed.

        Without this, a probe that always returned FAILED would satisfy every failure test
        above while proving nothing about the healthy path.
        """
        from knowledge_assistant.application.health import HealthService  # noqa: PLC0415

        probe = DatabaseProbe(CountingDatabase(), _fixed_clock())
        service = HealthService([probe], clock=_fixed_clock(), timeout_seconds=1.0)

        report = await service.report()

        assert report.ready is True
        assert report.probes[0].status is ProbeStatus.OK


class TestStartupFailure:
    """An unreachable database must fail the process start, not degrade it."""

    async def test_lifespan_raises_dependency_unavailable(self) -> None:
        """Scenario: the pool cannot be opened at startup.

        Phase 0 requires a dependency failure to become a *startup* failure rather than a
        runtime one, so a supervisor restarts rather than routing traffic to a process
        whose every request will fail.
        """
        from knowledge_assistant.lifespan import make_lifespan  # noqa: PLC0415

        database = CountingDatabase(
            failure=OperationalError("connection refused"),
        )
        lifespan = make_lifespan(database)

        with pytest.raises(DependencyUnavailableError):
            async with lifespan(object()):
                pass  # pragma: no cover - the yield must not be reached

    async def test_shutdown_closes_the_pool_even_after_a_successful_start(self) -> None:
        """A clean start followed by shutdown must close the pool.

        Leaking the pool on shutdown is how a redeploy exhausts PostgreSQL's connection
        limit, and it fails long after the change that caused it.
        """
        from knowledge_assistant.lifespan import make_lifespan  # noqa: PLC0415

        database = CountingDatabase()
        lifespan = make_lifespan(database)

        async with lifespan(object()):
            pass

        assert database.closed is True


def _fixed_clock() -> FixedClock:
    """Return a fixed clock for probe construction.

    Returns:
        A ``FixedClock`` at the shared test epoch.

    """
    return FixedClock(TEST_EPOCH)

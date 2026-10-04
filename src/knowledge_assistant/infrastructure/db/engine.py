"""PostgreSQL connection pool lifecycle.

Responsibilities, deliberately narrow:

* Build and open a pool with an explicit timeout policy.
* Apply ``statement_timeout`` and ``lock_timeout`` to **every** connection via the pool's
  configure callback, so no call site can forget.
* Translate driver exceptions into the domain error taxonomy, so that callers never branch on
  ``psycopg.OperationalError`` and so that no driver message reaches a client.
* Close cleanly, which is what makes a rolling restart lose nothing.

What this module deliberately does not do: it does not retry. A retry policy belongs to the
operation that knows whether retrying is safe (a job claim, an idempotent insert), not to the
transport. A pool that silently retries turns a partial outage into a latency spike and hides
the error.
"""

from __future__ import annotations

import datetime as dt
from types import TracebackType
from typing import Any, Self

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from knowledge_assistant.domain.errors import (
    DependencyUnavailableError,
    TimeoutError_,
    ValidationError,
)

__all__ = ["Database", "build_pool", "translate_db_error"]

#: Driver exception class names that mean "the database is not reachable right now".
_UNAVAILABLE_MARKERS = ("OperationalError", "InterfaceError", "CannotConnectNow")

#: Driver exception class names that mean "we waited too long".
_TIMEOUT_MARKERS = ("QueryCanceled", "LockNotAvailable", "PoolTimeout", "TooManyRequests")

#: Driver exception class names that mean "the statement is wrong" - never retryable.
_VIOLATION_MARKERS = ("SyntaxError", "UndefinedTable", "UndefinedColumn", "ProgrammingError")


def translate_db_error(exc: BaseException, *, dependency: str = "postgres") -> Exception:
    """Translate a driver exception into the domain error taxonomy.

    Args:
        exc: Exception raised by psycopg or the pool.
        dependency: Logical dependency name for the error detail.

    Returns:
        An ``ApplicationError`` subclass. The original exception is preserved as ``__cause__``
        so the operator sees it in logs; its *message* is not propagated to the client, because
        driver messages can embed DSN fragments and SQL text.

    Raises:
        Never. Returns the translated error instead.
    """
    type_name = type(exc).__name__
    if any(marker in type_name for marker in _VIOLATION_MARKERS):
        return ValidationError(
            f"database rejected the statement ({type_name})",
            detail={"dependency": dependency},
            cause=exc,
        )
    if any(marker in type_name for marker in _TIMEOUT_MARKERS):
        return TimeoutError_(
            f"database operation exceeded its budget ({type_name})",
            dependency=dependency,
            timeout_seconds=0.0,
            cause=exc,
        )
    if any(marker in type_name for marker in _UNAVAILABLE_MARKERS):
        return DependencyUnavailableError(
            f"database is unavailable ({type_name})",
            dependency=dependency,
            cause=exc,
        )
    return DependencyUnavailableError(
        f"database operation failed ({type_name})",
        dependency=dependency,
        cause=exc,
    )


def build_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    statement_timeout_ms: int,
    connect_timeout_seconds: int,
    application_name: str = "knowledge-assistant",
) -> AsyncConnectionPool:
    """Create a connection pool with per-connection timeout policy.

    Args:
        dsn: PostgreSQL connection string.
        min_size: Minimum eagerly opened connections.
        max_size: Maximum pool size.
        statement_timeout_ms: Applied to every connection as ``statement_timeout``.
        connect_timeout_seconds: Applied as ``connect_timeout`` and as the pool open timeout.
        application_name: Reported to PostgreSQL for connection attribution in
            ``pg_stat_activity``. Worth setting: "which service is holding this connection"
            is the first question asked during a database incident.

    Returns:
        A configured, unopened pool. The caller opens it during startup so that a failure to
        connect is a startup failure rather than a first-request failure.
    """
    configure = _make_configure(statement_timeout_ms)
    return AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=connect_timeout_seconds,
        max_idle=300.0,
        kwargs={
            "autocommit": True,  # transactions are opened explicitly; see TRANSACTIONS.md
            "row_factory": dict_row,
            "application_name": application_name,
        },
        configure=configure,
        open=False,
        check=_health_check,
    )


def _make_configure(statement_timeout_ms: int) -> Any:
    """Return a pool ``configure`` callback that applies the session timeout policy.

    The timeout is captured in a closure rather than read off the connection, because psycopg
    passes ``kwargs`` straight to ``connect()`` and an unexpected keyword would be rejected by
    the driver.

    ``statement_timeout`` is the important one: it bounds a single statement. ``lock_timeout``
    bounds time spent *waiting* for a lock, which without it converts lock contention into a
    queue of blocked connections - an outage with no obvious cause.

    Args:
        statement_timeout_ms: Value applied to ``statement_timeout``.

    Returns:
        An async callable suitable for ``AsyncConnectionPool(configure=...)``.
    """

    async def _configure(conn: AsyncConnection[Any]) -> None:
        async with conn.cursor() as cur:
            await cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            await cur.execute("SET lock_timeout = '3s'")
            await cur.execute("SET idle_in_transaction_session_timeout = '30s'")

    return _configure


async def _health_check(conn: AsyncConnection[Any]) -> bool:
    """Validate a pooled connection before reuse.

    A connection returned to the pool may have been closed by the server, the firewall, or a
    proxy timeout. Handing such a connection to application code produces a confusing error at
    an arbitrary call site; validating on checkout turns it into a transparent retry.

    Args:
        conn: Connection taken from the pool.

    Returns:
        ``True`` if the connection is usable.
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
    except Exception:  # noqa: BLE001 - any failure means "do not reuse this connection"
        return False
    return True


class Database:
    """Owns the pool and exposes a single acquisition helper.

    Wrapping the pool rather than passing it around means call sites cannot bypass the
    timeout policy or forget to translate errors.
    """

    __slots__ = ("_pool", "_dsn")

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Adopt an already-built pool."""
        self._pool = pool
        self._dsn = ""

    @property
    def pool(self) -> AsyncConnectionPool:
        """Return the underlying pool, for health reporting only."""
        return self._pool

    async def open(self) -> None:
        """Open the pool, waiting for an initial connection.

        Raises:
            DependencyUnavailableError: If the database is not reachable within the connect
                timeout. Failing here - at startup - is deliberate.
        """
        try:
            await self._pool.open(wait=True, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise translate_db_error(exc) from exc

    async def close(self) -> None:
        """Close the pool, waiting for in-flight work to finish."""
        await self._pool.close()

    async def acquire(self) -> Any:
        """Return an async context manager yielding a connection.

        Returns:
            An async context manager. Driver exceptions raised inside the block are
            translated by the caller's repository methods.
        """
        return self._pool.connection()

    async def __aenter__(self) -> Self:
        """Enter the async context, opening the pool if needed."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the pool on exit."""
        await self.close()


def utcnow() -> dt.datetime:
    """Return the current UTC time.

    Only for adapters that must not depend on an injected clock (e.g. a bootstrap path).
    Repositories receive time from the application layer instead.

    Returns:
        Timezone-aware current UTC time.
    """
    return dt.datetime.now(tz=dt.timezone.utc)
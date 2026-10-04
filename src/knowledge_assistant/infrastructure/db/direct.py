"""Single-connection database adapter.

Exists because psycopg's :class:`AsyncConnectionPool` cannot run on Windows: its background
connection worker needs socket readiness notification, and the only Windows event loop that can
serve psycopg's async I/O at all - ``SelectorEventLoop`` - does not provide it for sockets. The
pool therefore times out with ``PoolTimeout`` after thirty seconds, on a healthy server, with
a direct connection to the same server succeeding instantly.

This adapter exposes the **same ``acquire()`` port** as the pooled ``Database`` in
``engine``, so every store built on that port - ``PgKnowledgeStore`` included - is unchanged.

**What it costs.** One connection, so no concurrency, and a query on one connection blocks the
others. That is the right trade for a one-shot CLI command and the wrong one for a served API,
which is why this is selected by platform rather than made the default. On Linux and macOS the
pooled adapter is used and this class is never constructed.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from knowledge_assistant.infrastructure.db.engine import translate_db_error

__all__ = ["DirectDatabase", "build_direct_database"]


class _ConnectionContext:
    """Async context manager yielding a fixed connection without closing it.

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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Leave the context, leaving the connection open.

        Args:
            exc_type: Exception class, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any.

        """


class DirectDatabase:
    """One-connection implementation of the database port."""

    __slots__ = ("_dsn", "_conn")

    def __init__(self, dsn: str) -> None:
        """Create the adapter, unopened.

        Args:
            dsn: Connection string.

        """
        self._dsn = dsn
        self._conn: Any = None

    @property
    def connected(self) -> bool:
        """Return whether a connection is open.

        Returns:
            ``True`` once :meth:`open` has succeeded.

        """
        return self._conn is not None

    async def open(self) -> None:
        """Open the connection.

        Raises:
            Exception: Translated into the error taxonomy when the server is unreachable.

        """
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415

        try:
            self._conn = await psycopg.AsyncConnection.connect(
                self._dsn, autocommit=True, connect_timeout=10, row_factory=dict_row
            )
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise translate_db_error(exc) from exc

    async def close(self) -> None:
        """Close the connection if one is open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def acquire(self) -> Any:
        """Return an async context manager yielding the connection.

        Returns:
            An async context manager.

        Raises:
            RuntimeError: If called before :meth:`open`.

        """
        if self._conn is None:
            msg = "DirectDatabase.acquire() called before open()"
            raise RuntimeError(msg)
        return _ConnectionContext(self._conn)


def build_direct_database(dsn: str) -> DirectDatabase:
    """Build an unopened :class:`DirectDatabase`.

    Args:
        dsn: Connection string.

    Returns:
        The adapter.

    """
    return DirectDatabase(dsn)

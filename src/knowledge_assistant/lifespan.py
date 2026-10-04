"""Lifespan wiring.

Kept separate from both ``main.py`` and ``container.py`` so the startup and shutdown sequence is
one readable function, and so it can be unit-tested without binding a socket.

**The ordering rules encoded here:**

1. Open the pool before declaring readiness. A process that reports ready without a database
   would answer requests that then fail, converting a startup failure into a runtime one.
2. On shutdown, close *after* the server has drained in-flight requests, so an acknowledged
   request is never cut off mid-flight.
3. Close the pool even if the startup partially succeeded, otherwise a failure during startup
   leaks connections and the retry loop opens more.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from knowledge_assistant.domain.errors import DependencyUnavailableError
from knowledge_assistant.infrastructure.db.engine import Database
from knowledge_assistant.observability.logging import get_logger

__all__ = ["make_lifespan"]


def make_lifespan(database: Database) -> object:
    """Return an async context manager for FastAPI lifespan events.

    Args:
        database: Database handle whose pool is opened on startup and closed on shutdown.

    Returns:
        An async context manager suitable for ``FastAPI(lifespan=...)`` or for assigning to
        ``app.router.lifespan_context``.
    """
    logger = get_logger("lifespan")

    @contextlib.asynccontextmanager
    async def lifespan(_app: object) -> AsyncIterator[None]:
        """Open resources on startup and release them on shutdown.

        Args:
            _app: The FastAPI application. Unused; the signature is fixed by Starlette.

        Yields:
            ``None`` once the database pool is open.

        Raises:
            DependencyUnavailableError: If the pool cannot be opened, so the process fails to
                start rather than starting unhealthy.
        """
        try:
            await database.open()
        except Exception as exc:  # noqa: BLE001 - normalised below
            logger.critical("lifespan.database_open_failed", error_type=type(exc).__name__)
            raise DependencyUnavailableError(
                "database unavailable at startup", dependency="postgres", cause=exc
            ) from exc
        logger.info("lifespan.started")
        try:
            yield
        finally:
            await database.close()
            logger.info("lifespan.stopped")

    return lifespan
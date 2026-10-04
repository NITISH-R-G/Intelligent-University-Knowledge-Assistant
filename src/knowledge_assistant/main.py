"""API process entrypoint.

Deliberately thin: build the container, serve, shut down cleanly.

Startup opens the pool eagerly so that a database which is unreachable produces a **startup
failure** - which a supervisor should act on - rather than a first-request failure that a load
balancer has already routed traffic to. This is the Phase 0 requirement that a dependency
failure must not become a silent partial outage, applied at the earliest possible moment.
"""

from __future__ import annotations

import sys

import uvicorn

from knowledge_assistant.config.settings import load_settings
from knowledge_assistant.container import build_api_container
from knowledge_assistant.observability.logging import get_logger

__all__ = ["main"]


def main() -> int:
    """Run the API process.

    Returns:
        ``0`` on clean shutdown, ``2`` on configuration failure.

    """
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - configuration failure is a startup failure
        # Structured logging is not configured yet, so this one line goes to stderr as plain
        # text. Emitting JSON through a half-configured logger is worse than plain text.
        print(f"configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    container = build_api_container(settings)
    logger = get_logger("main")
    logger.info(
        "api.starting",
        host=settings.host,
        port=settings.port,
        environment=settings.environment.value,
    )
    try:
        uvicorn.run(
            container.app,
            host=settings.host,
            port=settings.port,
            log_config=None,  # structlog owns logging; uvicorn must not replace it
            access_log=False,  # replaced by the structured access-log middleware
            timeout_graceful_shutdown=30,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        logger.info("api.interrupted")
    finally:
        logger.info("api.stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

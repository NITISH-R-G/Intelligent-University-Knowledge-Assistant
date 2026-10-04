"""Worker process entrypoint.

Startup mirrors the API: open the pool eagerly, fail to start if the database is unreachable.
A worker that starts without its queue and then retries forever is worse than one that exits
and gets restarted by a supervisor, because the exit is visible and the retry loop is not.

``--once`` drains a single claim and exits, which is what the failure-injection tests and the
Definition-of-Done checks use: a bounded, deterministic unit of work.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from knowledge_assistant.config.settings import load_settings
from knowledge_assistant.container import build_worker_container
from knowledge_assistant.domain.errors import ApplicationError
from knowledge_assistant.observability.logging import get_logger

__all__ = ["main", "run_worker"]


async def run_worker(*, once: bool = False, max_iterations: int | None = None) -> int:
    """Open the pool, install signal handlers and run the loop.

    Args:
        once: Execute a single claim then exit.
        max_iterations: Stop after this many polls.

    Returns:
        Process exit code: ``0`` clean, ``2`` configuration failure, ``3`` dependency failure.
    """
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - configuration failure is a startup failure
        print(f"configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    container = build_worker_container(settings)
    logger = get_logger("worker_main")
    try:
        await container.database.open()
    except ApplicationError as exc:
        logger.critical("worker.database_unavailable_at_startup", error_type=type(exc).__name__)
        return 3

    runner = container.runner
    try:
        runner.install_signal_handlers()
        await runner.run_forever(max_iterations=1 if once else max_iterations)
        return 0
    finally:
        await container.database.close()
        logger.info("worker.database_closed")


def main() -> int:
    """Command-line entrypoint for the worker process.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(prog="ka-worker", description="Knowledge Assistant worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job then exit (used by tests and definition-of-done checks).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop after this many poll iterations.",
    )
    args = parser.parse_args()
    return asyncio.run(run_worker(once=args.once, max_iterations=args.max_iterations))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
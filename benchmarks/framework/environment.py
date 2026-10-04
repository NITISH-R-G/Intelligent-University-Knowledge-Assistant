"""Environment capture for benchmark runs.

A latency number is meaningless without the machine, interpreter and dependency set that
produced it. "p95 = 4.2 ms" measured on a developer laptop with a warm page cache is not
a claim about anything; it is a claim about that laptop, and only if the reader is told
which one.

Everything here is local. No network call, no telemetry, no cloud metadata service: the
project has a zero-cost constraint, and a benchmark harness that phones home is both a
cost and a privacy problem for a tool whose entire job is reproducibility.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Final

__all__ = ["capture_environment", "current_commit_sha", "is_worktree_dirty"]

#: Distribution names recorded for every run. Kept short on purpose: the lockfile is the
#: authoritative full set, and a result carrying 47 versions is unreadable. These are the
#: ones that plausibly move a latency number.
_TRACKED_DISTRIBUTIONS: Final[tuple[str, ...]] = (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-settings",
    "uvicorn",
    "structlog",
    "psycopg",
    "sqlalchemy",
    "alembic",
    "httpx",
)


def current_commit_sha() -> str:
    """Return the current Git commit SHA, or a marker when it cannot be determined.

    Returns:
        The full SHA, or ``"unknown"``. Never raises: an unavailable Git must not stop a
        benchmark run, it must be visible in the result instead.

    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=Path.cwd(),
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def is_worktree_dirty() -> bool:
    """Return whether the working tree has uncommitted changes.

    A dirty tree means the recorded SHA does not fully describe the code that ran, which
    invalidates comparison against any baseline. Recording the flag lets a reader see
    that rather than assume.

    Returns:
        ``True`` when ``git status --porcelain`` reports anything.

    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=Path.cwd(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    return bool(completed.stdout.strip())


def _gil_state() -> str:
    """Report whether the global interpreter lock is active.

    Recorded because a free-threaded build would make single-threaded timings meaningless
    as a comparison, and a future reader needs to know which build produced a baseline.

    Returns:
        ``"true"`` when the GIL is enabled, ``"false"`` when not, ``"unknown"`` on an
        interpreter that does not expose the check.

    """
    probe = getattr(sys, "_is_gil_enabled", None)
    if not callable(probe):
        return "unknown"
    return str(not bool(probe())).lower()


def capture_environment() -> dict[str, str]:
    """Collect the metadata that makes a measurement interpretable.

    Returns:
        Flat string mapping. Empty values are omitted rather than recorded as
        ``"unknown"``, so a reader can tell "not available here" from "the string
        literally was unknown".

    """
    environment: dict[str, str] = {
        "commit_sha": current_commit_sha(),
        "worktree_dirty": str(is_worktree_dirty()).lower(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": str(os.cpu_count() or 0),
        "gil_enabled": _gil_state(),
    }
    for distribution in _TRACKED_DISTRIBUTIONS:
        try:
            environment[f"pkg_{distribution}"] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return environment

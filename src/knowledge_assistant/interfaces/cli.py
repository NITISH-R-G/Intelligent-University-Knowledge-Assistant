"""Developer CLI: the portable equivalent of ``make``.

The canonical developer entrypoint is ``make up`` (see ``Makefile``). ``make`` is not present
on every machine - notably most Windows shells without a Unix toolchain - so the same targets
are available through this module with no dependency beyond Python itself:

.. code-block:: bash

    python -m knowledge_assistant.interfaces.cli up        # equivalent to: make up
    python -m knowledge_assistant.interfaces.cli test      # equivalent to: make test
    python -m knowledge_assistant.interfaces.cli check     # equivalent to: make check

Having two implementations of the same commands is a duplication risk, so both call the same
underlying functions and both delegate the heavy lifting to ``scripts/``. ``make`` exists for
discoverability and convention; this module exists so the instructions work everywhere.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = ["main"]

#: Repository root, resolved from this file's location (src/knowledge_assistant/interfaces/).
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Interpreter used for subprocesses: the one running this CLI, so a virtualenv is respected.
PYTHON = sys.executable


def _run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
    """Run a subprocess and stream its output.

    Args:
        args: Command and arguments.
        cwd: Working directory. Defaults to the repository root.
        check: Raise ``CalledProcessError`` on a non-zero exit.

    Returns:
        The process exit code.

    """
    completed = subprocess.run(args, cwd=cwd or REPO_ROOT, check=False)  # noqa: S603
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, args)
    return completed.returncode


def _tool_available(name: str) -> bool:
    """Return whether an executable is on PATH.

    Args:
        name: Executable name.

    Returns:
        ``True`` if resolvable.

    """
    return shutil.which(name) is not None


def cmd_up() -> int:
    """Start the development stack.

    Prefers Docker Compose, which is the documented path. Falls back to a clear error with
    remediation rather than a partially started stack: a developer who sees five different
    failure modes for "the database is not running" learns nothing.

    Returns:
        Exit code.

    """
    if not _tool_available("docker"):
        print(
            "error: docker is required for `up`.\n"
            "  Install Docker Desktop, or run the database directly and start the API with:\n"
            "    python -m knowledge_assistant.interfaces.cli serve",
            file=sys.stderr,
        )
        return 1
    return _run(["docker", "compose", "up", "-d", "--wait"])


def cmd_down() -> int:
    """Stop the development stack and remove volumes."""
    return _run(["docker", "compose", "down", "-v"])


def cmd_logs() -> int:
    """Follow stack logs."""
    return _run(["docker", "compose", "logs", "-f"])


def cmd_serve() -> int:
    """Run the API process in the foreground."""
    return _run([PYTHON, "-m", "knowledge_assistant.main"])


def cmd_worker() -> int:
    """Run the worker process in the foreground."""
    return _run([PYTHON, "-m", "knowledge_assistant.worker_main"])


def cmd_migrate() -> int:
    """Apply database migrations."""
    return _run([PYTHON, "-m", "alembic", "upgrade", "head"])


def cmd_ingest(args: argparse.Namespace) -> int:
    """Load a corpus directory into the knowledge base.

    Delegates to ``knowledge_main`` by subprocess, exactly as ``serve`` and ``worker`` do. The
    knowledge commands need the composition root, and the architecture rules forbid the
    interface layer from importing it - which is the rule working as intended, not an
    obstacle to route around.
    """
    argv = [PYTHON, "-m", "knowledge_assistant.knowledge_main", "ingest"]
    if getattr(args, "corpus", None):
        argv += ["--corpus", args.corpus]
    return _run(argv)


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer one question from retrieved evidence."""
    argv = [PYTHON, "-m", "knowledge_assistant.knowledge_main", "ask", *args.question]
    if getattr(args, "verbose", False):
        argv.append("--verbose")
    return _run(argv)


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the fixed end-to-end demo script."""
    del args
    return _run([PYTHON, "-m", "knowledge_assistant.knowledge_main", "demo"])


def cmd_eval(args: argparse.Namespace) -> int:
    """Run the MVP retrieval evaluation."""
    argv = [PYTHON, "-m", "knowledge_assistant.knowledge_main", "eval"]
    if getattr(args, "path", None):
        argv += ["--path", args.path]
    return _run(argv)


def _taking_args(handler: Callable[[argparse.Namespace], int]) -> Callable[[], int]:
    """Adapt a handler that needs the parsed namespace to the zero-argument dispatcher contract.

    The developer commands predate the knowledge commands and all take no arguments, so
    ``main`` calls ``args.func()``. The knowledge commands need the namespace the parser just
    built. Wrapping here keeps both shapes working without changing the signature of any
    existing command.

    Args:
        handler: A handler that accepts the parsed namespace.

    Returns:
        A zero-argument callable that forwards the namespace.

    """

    def run() -> int:
        return handler(parse_args())

    return run


def cmd_test() -> int:
    """Run the test suite."""
    return _run([PYTHON, "-m", "pytest"])


def cmd_lint() -> int:
    """Run lint and format checks."""
    code = 0
    if _tool_available("ruff"):
        code |= _run([PYTHON, "-m", "ruff", "check", "."], check=False)
        code |= _run([PYTHON, "-m", "ruff", "format", "--check", "."], check=False)
    else:
        print("error: ruff not installed. Run: pip install -e .[dev]", file=sys.stderr)
        return 1
    return code


def cmd_typecheck() -> int:
    """Run the type checker."""
    if not _tool_available("mypy"):
        print("error: mypy not installed. Run: pip install -e .[dev]", file=sys.stderr)
        return 1
    return _run([PYTHON, "-m", "mypy"], check=False)


def cmd_arch() -> int:
    """Run architecture dependency-rule checks."""
    return _run([PYTHON, "scripts/check_architecture.py"])


def cmd_check() -> int:
    """Run every non-container check: format, lint, types, architecture, tests.

    Returns:
        ``0`` only if every stage passes. Stages are run in cheapest-first order so a
        formatting error is reported in seconds rather than after the full test suite.

    """
    failures: list[str] = []
    for name, fn in (
        ("lint", cmd_lint),
        ("typecheck", cmd_typecheck),
        ("architecture", cmd_arch),
        ("tests", cmd_test),
    ):
        print(f"\n=== {name} ===", flush=True)
        if fn() != 0:
            failures.append(name)
    if failures:
        print(f"\nFAILED stages: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


def cmd_dod() -> int:
    """Run the machine-checkable Phase 1 definition-of-done verification."""
    return _run([PYTHON, "scripts/definition_of_done.py"])


def build_parser() -> argparse.ArgumentParser:
    """Build the ``ka`` argument parser.

    Split out from :func:`main` so :func:`_taking_args` can re-read the parsed namespace in the
    handler closure without threading the parser through every command.

    Returns:
        The configured parser.

    """
    parser = argparse.ArgumentParser(prog="ka", description="Knowledge Assistant developer CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("up", cmd_up, "Start the stack (docker compose)"),
        ("down", cmd_down, "Stop the stack and remove volumes"),
        ("logs", cmd_logs, "Follow stack logs"),
        ("serve", cmd_serve, "Run the API in the foreground"),
        ("worker", cmd_worker, "Run the worker in the foreground"),
        ("migrate", cmd_migrate, "Apply database migrations"),
        ("test", cmd_test, "Run the test suite"),
        ("lint", cmd_lint, "Lint and format check"),
        ("typecheck", cmd_typecheck, "Run mypy"),
        ("arch", cmd_arch, "Run architecture dependency checks"),
        ("check", cmd_check, "Run lint, typecheck, architecture and tests"),
        ("dod", cmd_dod, "Run the definition-of-done verification"),
    ):
        sub.add_parser(name, help=help_text).set_defaults(func=handler)

    # Knowledge commands take arguments, so they are registered separately rather than by the
    # zero-argument loop above.
    ingest = sub.add_parser("ingest", help="Load a corpus directory into the knowledge base")
    ingest.add_argument("--corpus", default=None)
    ingest.set_defaults(func=_taking_args(cmd_ingest))
    ask = sub.add_parser("ask", help="Answer one question from retrieved evidence")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--verbose", action="store_true")
    ask.set_defaults(func=_taking_args(cmd_ask))
    sub.add_parser("demo", help="Run the fixed end-to-end demo").set_defaults(
        func=_taking_args(cmd_demo)
    )
    evaluate = sub.add_parser("eval", help="Run the MVP retrieval evaluation")
    evaluate.add_argument("--path", default=None)
    evaluate.set_defaults(func=_taking_args(cmd_eval))
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse ``ka`` arguments.

    Args:
        argv: Argument vector, or ``None`` to read ``sys.argv``.

    Returns:
        The parsed namespace.

    """
    return build_parser().parse_args(argv)


def main() -> int:
    """Parse arguments and dispatch.

    Returns:
        Process exit code.

    """
    args = build_parser().parse_args()
    return int(args.func())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

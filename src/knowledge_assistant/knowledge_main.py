"""CLI commands for the retrieval-augmented generation slice.

Four commands, and they are the whole demo:

* ``ka ingest`` - load a corpus directory into PostgreSQL with vectors.
* ``ka ask "..."`` - answer one question from retrieved evidence, with sources.
* ``ka demo`` - run a fixed script of questions, including one the corpus cannot answer.
* ``ka eval`` - run the MVP retrieval evaluation set and print hit rate.

They live here rather than in a script because the composition root already knows how to wire
every collaborator, and a second entry point would mean a second wiring that can drift.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from knowledge_assistant.config.settings import load_settings
from knowledge_assistant.container import build_knowledge_container

__all__ = [
    "DEFAULT_CORPUS_DIR",
    "cmd_ingest",
    "cmd_ask",
    "cmd_demo",
    "cmd_eval",
    "DEMO_QUESTIONS",
]

#: The corpus that ships with the repository. Chosen as a default so ``ka ingest`` works with
#: no arguments, which is the difference between a demo someone runs and one they read about.
DEFAULT_CORPUS_DIR = Path("corpus/knowledge")

#: Questions exercised by ``ka demo``. The last one is deliberately unanswerable: a demo that
#: only shows successes does not demonstrate the refusal path, which is the safety property.
DEMO_QUESTIONS: tuple[tuple[str, bool], ...] = (
    ("What are the library timings?", True),
    ("What is the attendance requirement?", True),
    ("How do I apply for an OD request?", True),
    ("Who should I contact for academic support?", True),
    ("What is the university policy on bringing pets into the laboratory?", False),
)

#: Width of the rule used to underline the answer in terminal output.
_RULE = "-" * 78


def _corpus_dir(value: str | None) -> Path:
    """Resolve the corpus directory.

    Args:
        value: Optional path supplied on the command line.

    Returns:
        The directory to ingest.

    """
    return Path(value) if value else DEFAULT_CORPUS_DIR


async def _run_ingest(directory: Path, *, pattern: str = "*.md") -> int:
    """Ingest a corpus and print what happened.

    Args:
        directory: Corpus root.
        pattern: Glob applied to files.

    Returns:
        Process exit code.

    """
    container = await build_knowledge_container(load_settings())
    try:
        report = await container.ingest.ingest_directory(directory, pattern=pattern)
        stats = await container.store.stats()
        print(f"Ingested {report.documents} documents into {stats.chunks} chunks.")
        print(f"  provider : {container.embedder.name} ({container.embedder.dimensions}d)")
        print(f"  corpus   : {directory}")
        for document_id in report.ids:
            print(f"    - {document_id}")
    finally:
        await container.aclose()
    return 0


def cmd_ingest(args: Any) -> int:
    """Ingest a corpus directory.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.

    """
    return _run_coroutine(_run_ingest(_corpus_dir(getattr(args, "corpus", None))))


def _print_answer(question: str, result: Any, *, verbose: bool = False) -> None:
    """Print one answer and its sources.

    Args:
        question: The question asked.
        result: The :class:`KnowledgeAnswer` returned by the use case.
        verbose: Whether to print the assembled prompt and the rejected candidates.

    """
    print(_RULE)
    print(f"Q: {question}")
    print(_RULE)
    print(result.answer)
    print()
    if result.sources:
        print(f"Sources ({len(result.sources)}):")
        for source in result.sources:
            section = f" > {source.section}" if source.section else ""
            print(f"  [{source.rank}] {source.document}{section}  (score {source.score:.3f})")
            print(f"      chunk {source.chunk_id}  from {source.source}")
    else:
        print("Sources: none - the assistant declined to answer from the corpus.")
    if verbose:
        print()
        print("--- prompt ---")
        print(result.prompt)
        print()
        print("--- retrieved (including below-threshold candidates) ---")
        for hit in result.retrieved:
            print(f"  {hit.score:.4f}  {hit.id}  (v={hit.vector_rank} l={hit.lexical_rank})")
    print(_RULE)
    print()


async def _run_ask(question: str, *, verbose: bool = False) -> int:
    """Answer one question.

    Args:
        question: The question to answer.
        verbose: Whether to print the prompt and retrieval trace.

    Returns:
        Process exit code.

    """
    container = await build_knowledge_container(load_settings())
    try:
        result = await container.answer.answer(question)
        _print_answer(question, result, verbose=verbose)
    finally:
        await container.aclose()
    return 0


def cmd_ask(args: Any) -> int:
    """Answer a single question.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.

    """
    return _run_coroutine(
        _run_ask(" ".join(args.question), verbose=bool(getattr(args, "verbose", False)))
    )


async def _run_demo() -> int:
    """Answer the fixed demo script.

    Returns:
        ``0`` when the refusal path behaved as expected, ``1`` otherwise.

    """
    container = await build_knowledge_container(load_settings())
    problems: list[str] = []
    try:
        stats = await container.store.stats()
        print(
            f"Knowledge base: {stats.documents} documents, {stats.chunks} chunks, "
            f"embedding provider {container.embedder.name}, "
            f"generator {container.generator.name}"
        )
        print()
        for question, expect_answer in DEMO_QUESTIONS:
            result = await container.answer.answer(question)
            _print_answer(question, result)
            if expect_answer and not result.grounded:
                problems.append(f"expected an answer for: {question}")
            if not expect_answer and result.grounded:
                problems.append(f"expected a refusal for: {question}")
    finally:
        await container.aclose()
    if problems:
        print("DEMO FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        "Demo complete: grounded answers cited sources, and the unanswerable question was refused."
    )
    return 0


def cmd_demo(args: Any) -> int:
    """Run the fixed demo script.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.

    """
    del args
    return _run_coroutine(_run_demo())


async def _run_eval(path: Path) -> int:
    """Run the MVP retrieval evaluation set.

    Args:
        path: JSON file holding the evaluation set.

    Returns:
        Process exit code.

    """
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    container = await build_knowledge_container(load_settings())
    try:
        hits = 0
        grounded = 0
        refusals_correct = 0
        refusals = 0
        misses: list[str] = []
        for case in cases:
            result = await container.answer.answer(case["question"])
            documents = [source.document for source in result.sources]
            expected = case.get("expect_document")
            found = expected is None or any(expected.lower() in d.lower() for d in documents)
            if found:
                hits += 1
            else:
                misses.append(f"{case['question']} -> expected {expected}, got {documents}")
            if result.grounded:
                grounded += 1
            else:
                refusals += 1
                if expected is None:
                    refusals_correct += 1

        total = len(cases)
        print(f"MVP retrieval evaluation - {path.name}")
        print(f"  cases                  : {total}")
        print(f"  retrieval hit rate     : {hits / total:.0%} ({hits}/{total})")
        print(f"  grounded answers       : {grounded}/{total}")
        print(f"  refusals               : {refusals}/{total}")
        if refusals:
            print(f"  correct refusals       : {refusals_correct}/{refusals}")
        for miss in misses:
            print(f"  MISS: {miss}")
    finally:
        await container.aclose()
    return 0


def cmd_eval(args: Any) -> int:
    """Run the MVP retrieval evaluation.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.

    """
    path = Path(getattr(args, "path", None) or "eval/retrieval_eval.json")
    if not path.exists():
        print(f"error: evaluation file not found: {path}", file=sys.stderr)
        return 2
    return _run_coroutine(_run_eval(path))


def build_parser() -> Any:
    """Return the argument parser for the knowledge commands.

    Returns:
        A configured parser.

    """
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="ka-knowledge", description="University knowledge assistant demo"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Load a corpus directory into the knowledge base")
    ingest.add_argument("--corpus", default=None, help="Corpus directory")
    ingest.set_defaults(func=cmd_ingest)
    ask = sub.add_parser("ask", help="Answer one question from retrieved evidence")
    ask.add_argument("question", nargs="+", help="The question to answer")
    ask.add_argument("--verbose", action="store_true", help="Show prompt and retrieval trace")
    ask.set_defaults(func=cmd_ask)
    sub.add_parser("demo", help="Run the fixed demo script").set_defaults(func=cmd_demo)
    evaluate = sub.add_parser("eval", help="Run the MVP retrieval evaluation")
    evaluate.add_argument("--path", default=None, help="Evaluation set JSON file")
    evaluate.set_defaults(func=cmd_eval)
    serve = sub.add_parser("serve", help="Serve POST /api/v1/knowledge/query over HTTP")
    serve.add_argument("--host", default=None, help="Bind address; defaults to KA_HOST")
    serve.add_argument("--port", type=int, default=None, help="Port; defaults to KA_PORT")
    serve.set_defaults(func=cmd_serve)
    return parser


def cmd_serve(args: Any) -> int:
    """Serve the knowledge query endpoint.

    Runs the real HTTP surface rather than describing it: the same application object the CLI
    uses is handed to uvicorn, so the ``curl`` in the README exercises the shipped route.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.

    """
    import uvicorn  # noqa: PLC0415

    from knowledge_assistant.config.settings import load_settings  # noqa: PLC0415
    from knowledge_assistant.container import build_knowledge_app  # noqa: PLC0415

    settings = load_settings()
    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port

    # The database connection is opened by the application's startup hook rather than here, so
    # it is bound to the loop uvicorn will actually serve on.
    app = build_knowledge_app(settings)

    print(f"Knowledge API on http://{host}:{port}/api/v1/knowledge/query")  # noqa: T201
    if settings.require_authentication:
        # The Phase 0 boundary fails closed for every path outside PUBLIC_PATHS, and the query
        # endpoint is deliberately not on that list: it is unauthenticated only because there is
        # no authentication implementation yet, not because it is safe to expose. Saying so at
        # startup turns a confusing 403 into the one line that fixes it.
        print(  # noqa: T201
            "WARNING: KA_REQUIRE_AUTHENTICATION is true, so every query returns 403. "
            "For a local demo set KA_REQUIRE_AUTHENTICATION=false. This is refused by "
            "configuration validation in staging and production."
        )
    # ``loop="none"`` plus awaiting ``serve()`` inside a loop we choose ourselves, rather than
    # ``uvicorn.run``. uvicorn's own runner installs a Proactor loop on Windows regardless of the
    # event-loop policy, and psycopg's async implementation refuses that loop outright - the same
    # limitation documented in infrastructure/db/direct.py. Owning the loop is the only way to
    # serve this API on Windows; on every other platform it is plain ``asyncio.run``.
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, loop="none")
    )
    if sys.platform == "win32":
        import selectors  # noqa: PLC0415

        asyncio.run(
            server.serve(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        asyncio.run(server.serve())
    return 0


def _run_coroutine(coro: Any) -> int:
    """Run a coroutine on an event loop the async driver can actually use.

    Windows' default ``ProactorEventLoop`` cannot serve psycopg's async I/O at all, and the
    ``SelectorEventLoop`` that can is not the default, so the loop is selected explicitly. On
    every other platform this is exactly ``asyncio.run``.

    ``main.py`` and ``worker_main.py`` have the same Windows limitation and are not changed
    here - that is a separate decision about which platforms are supported for serving.

    Args:
        coro: The coroutine to run.

    Returns:
        The coroutine's return value as an ``int``.

    """
    # The result is assigned in each branch and returned once at the end. An early `return`
    # inside the `if` would make the tail dead code under mypy's `warn_unreachable` when
    # type-checking on Windows, and an `else: return` would trip ruff's RET505.
    if sys.platform == "win32":
        import selectors  # noqa: PLC0415

        result = asyncio.run(
            coro, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        )
    else:
        result = asyncio.run(coro)
    return int(result)


def main() -> int:
    """Run the knowledge demo commands.

    Returns:
        Process exit code.

    """
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

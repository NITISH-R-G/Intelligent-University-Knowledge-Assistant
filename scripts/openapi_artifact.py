"""Generate or verify the committed OpenAPI artefact.

Why a committed artefact rather than a served endpoint: the authentication boundary denies
every path outside the four public ones, so ``/openapi.json`` is not reachable by an anonymous
caller. Committing the generated document keeps the public surface minimal while still giving
consumers a stable contract to diff against.

Usage:
    python scripts/openapi_artifact.py --write    # regenerate the committed copy
    python scripts/openapi_artifact.py           # verify the committed copy is current
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

ARTEFACT = REPO_ROOT / "docs" / "07-api" / "openapi.json"

#: Settings used for generation. Deterministic so the artefact does not churn between runs
#: for reasons that have nothing to do with the API surface.
_GENERATION_SETTINGS: dict[str, Any] = {
    "environment": "test",
    "database_url": "postgresql://unused:unused@localhost:5432/unused",
    "service_name": "knowledge-assistant",
}


def build_document() -> dict[str, Any]:
    """Build the OpenAPI document from the real application factory.

    Returns:
        The specification as a mapping.

    Raises:
        SystemExit: If configuration or application construction fails, which would mean the
            committed artefact was generated from a different application than the one that
            ships.

    """
    import datetime as dt  # noqa: PLC0415

    from knowledge_assistant.application.health import HealthService  # noqa: PLC0415
    from knowledge_assistant.config.settings import Settings  # noqa: PLC0415
    from knowledge_assistant.domain.clock import UTC, FixedClock  # noqa: PLC0415
    from knowledge_assistant.domain.health import (  # noqa: PLC0415
        ProbeCriticality,
        ProbeResult,
        ProbeStatus,
    )
    from knowledge_assistant.interfaces.http.app import create_app  # noqa: PLC0415

    class _StaticProbe:
        """Probe that always succeeds, so generation needs no database."""

        name = "database"
        criticality = ProbeCriticality.CRITICAL

        async def check(self) -> ProbeResult:
            """Return a healthy result.

            Returns:
                A passing probe result.

            """
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.OK,
                criticality=self.criticality,
                detail="ok",
            )

    settings = Settings(**_GENERATION_SETTINGS)
    clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=UTC))
    app = create_app(
        health=HealthService([_StaticProbe()], clock=clock),
        service_name=settings.service_name,
        settings_public=settings.public_summary(),
        clock=clock,
    )
    return dict(app.openapi())


def render(document: dict[str, Any]) -> str:
    """Render the document deterministically.

    Args:
        document: Specification mapping.

    Returns:
        Pretty-printed JSON with a trailing newline.

    """
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the generator or verifier.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the committed artefact is missing or stale.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="rewrite the committed artefact instead of verifying"
    )
    args = parser.parse_args(argv)

    document = build_document()
    rendered = render(document)

    if args.write:
        ARTEFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTEFACT.write_text(rendered, encoding="utf-8")
        print(f"wrote {ARTEFACT.relative_to(REPO_ROOT)}")
        return 0

    if not ARTEFACT.exists():
        print(
            f"error: {ARTEFACT.relative_to(REPO_ROOT)} is missing. "
            "Run: python scripts/openapi_artifact.py --write",
            file=sys.stderr,
        )
        return 1

    committed = ARTEFACT.read_text(encoding="utf-8")
    if json.loads(committed) != document:
        print(
            "error: the committed OpenAPI document does not match the application.\n"
            "       This is an API contract change. Review it deliberately, then run:\n"
            "       python scripts/openapi_artifact.py --write",
            file=sys.stderr,
        )
        return 1

    paths = ", ".join(sorted(document.get("paths", {})))
    print(f"openapi: OK - {len(document.get('paths', {}))} paths ({paths})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

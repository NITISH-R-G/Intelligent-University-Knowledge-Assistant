"""Static cross-check: does .env.example describe the stack docker-compose.yml builds.

Run without a Docker daemon. Everything a live integration test would prove about
*connectivity* needs a database, but the wiring between the two configuration
sources does not: if the DSN in `.env.example` names a user, password, database or
port that the compose file does not create, then `make up` followed by `make migrate`
fails at the first connection for a reason that has nothing to do with the
application.

That mismatch is currently invisible. `.env.example` is documentation and
docker-compose.yml is configuration, nothing cross-checks them, and both are easy to
edit independently. This script closes that gap without pretending to be an
integration test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from psycopg.conninfo import make_conninfo  # noqa: E402

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_DSN = re.compile(
    r"^postgresql://(?P<user>[^:]+):(?P<password>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)/(?P<db>.+)$"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _declared_env() -> dict[str, str]:
    """Return the active assignments in `.env.example`."""
    declared: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            match = _ASSIGNMENT.match(stripped)
            if match:
                declared[match.group("key")] = match.group("value")
    return declared


def _compose_defaults() -> dict[str, str]:
    """Return the `${VAR:-default}` values the compose file substitutes.

    Reads the defaults rather than assuming them, so renaming a variable in the compose
    file makes this return nothing for it rather than silently comparing against a
    stale hard-coded value here.

    Returns:
        Mapping of compose variable name to its default value.

    """
    text = COMPOSE.read_text(encoding="utf-8")
    return dict(re.findall(r"\$\{(POSTGRES_\w+):-([^}]*)\}", text))


def main() -> int:
    """Compare the two sources and report mismatches.

    Returns:
        ``0`` when consistent, ``1`` otherwise.

    """
    env = _declared_env()
    compose = _compose_defaults()
    problems: list[str] = []

    dsn = env.get("KA_DATABASE_URL", "")
    match = _DSN.match(dsn)
    if match is None:
        problems.append(f"KA_DATABASE_URL is not a plain postgresql:// DSN: {dsn!r}")
        print("env/compose: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    try:
        make_conninfo(dsn)
    except Exception as exc:  # noqa: BLE001 - any failure means the DSN is unusable
        problems.append(f"psycopg cannot parse the DSN: {exc}")

    pairs = (
        ("user", "POSTGRES_USER"),
        ("password", "POSTGRES_PASSWORD"),
        ("db", "POSTGRES_DB"),
    )
    for dsn_key, compose_key in pairs:
        declared = match.group(dsn_key)
        built = env.get(compose_key) or compose.get(compose_key)
        if built is not None and declared != built:
            problems.append(f"KA_DATABASE_URL {dsn_key}={declared!r} but {compose_key}={built!r}")

    port = env.get("POSTGRES_PORT") or compose.get("POSTGRES_PORT")
    if port is not None and match.group("port") != port:
        problems.append(f"KA_DATABASE_URL port={match.group('port')} but POSTGRES_PORT={port}")

    # The compose file publishes on loopback only, so a DSN naming any other host
    # describes a container-to-container path that this stack does not provide.
    if match.group("host") not in {"127.0.0.1", "localhost"}:
        problems.append(
            f"KA_DATABASE_URL host={match.group('host')!r}; docker-compose.yml "
            "publishes PostgreSQL on loopback only"
        )

    if problems:
        print("env/compose: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        "env/compose: OK - DSN "
        f"{match.group('user')}@localhost:{match.group('port')}/{match.group('db')} "
        "matches the compose service (host, user, password, database, port)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

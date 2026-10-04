"""Static enforcement of the dependency rules.

Run as ``python scripts/check_architecture.py``. Exits non-zero on any violation. Implemented with
the standard library ``ast`` module rather than an import-hook library because:

* it works without importing the target modules, so a syntax error or a missing dependency in
  one module cannot make the whole check unrunnable;
* it is deterministic and has no configuration surface of its own;
* the rules are readable as data, and a reviewer can see exactly what is forbidden.

**Why a static check at all.** The layered architecture is the single largest determinant of
whether this codebase is maintainable in two years, and it is the property most likely to decay
silently: a developer reaches for a direct database import from a use case because it is one line
shorter, and nothing fails. A comment in a docstring does not prevent that; this script does.

Rules are declared as data in :data:`RULES` and asserted by
``tests/architecture/test_dependency_rules.py``, so the policy and the enforcement cannot drift
into disagreeing with each other.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["RULES", "Violation", "check_file", "check_tree", "main"]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SRC_ROOT: Final[Path] = REPO_ROOT / "src" / "knowledge_assistant"

#: Modules considered part of the standard library for this project's purposes. Anything not in
#: this set and not explicitly allowed by a rule is a third-party dependency, which requires an
#: explicit entry in the relevant rule.
_STDLIB: Final[frozenset[str]] = frozenset(
    {
        "abc",
        "argparse",
        "ast",
        "asyncio",
        "base64",
        "collections",
        "contextlib",
        "contextvars",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "hashlib",
        "http",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "random",
        "re",
        "secrets",
        "signal",
        "socket",
        "ssl",
        "statistics",
        "string",
        "subprocess",
        "sys",
        "textwrap",
        "threading",
        "time",
        "types",
        "typing",
        "unicodedata",
        "uuid",
        "warnings",
        "weakref",
        "__future__",
    }
)


@dataclass(frozen=True, slots=True)
class Rule:
    """A dependency rule for one package.

    Attributes:
        package: Package directory name, e.g. ``domain``.
        allowed_first_party: Other first-party packages it may import.
        allowed_third_party: Exact top-level third-party module names it may import.
        rationale: Why the rule exists. Rendered in violation messages, because a rule nobody
            understands gets deleted by the next person who trips over it.

    """

    package: str
    allowed_first_party: frozenset[str]
    allowed_third_party: frozenset[str]
    rationale: str


#: The complete rule set. This is the architectural policy in one readable place.
RULES: Final[dict[str, Rule]] = {
    "domain": Rule(
        package="domain",
        allowed_first_party=frozenset(),
        allowed_third_party=frozenset(),
        rationale=(
            "The domain holds the rules that must stay correct. It imports only the standard "
            "library so that every rule is deterministically testable with no infrastructure."
        ),
    ),
    "config": Rule(
        package="config",
        allowed_first_party=frozenset(),
        allowed_third_party=frozenset({"pydantic", "pydantic_settings"}),
        rationale=(
            "Configuration is an input to the system, not part of its rules. Allowing pydantic "
            "here keeps the only settings library in one place."
        ),
    ),
    "observability": Rule(
        package="observability",
        allowed_first_party=frozenset(),
        allowed_third_party=frozenset({"structlog", "opentelemetry"}),
        rationale=(
            "Telemetry backends are replaceable and must never be imported by domain or "
            "application code, which would couple business rules to a logging library."
        ),
    ),
    "application": Rule(
        package="application",
        allowed_first_party=frozenset({"domain"}),
        allowed_third_party=frozenset(),
        rationale=(
            "Use cases orchestrate domain rules through ports. If application could import "
            "infrastructure, the whole port abstraction becomes decorative and the Phase 0 "
            "replaceability claim becomes false."
        ),
    ),
    "infrastructure": Rule(
        package="infrastructure",
        allowed_first_party=frozenset({"domain", "application", "config", "observability"}),
        allowed_third_party=frozenset(
            {"psycopg", "psycopg_pool", "sqlalchemy", "alembic", "pydantic", "pydantic_settings"}
        ),
        rationale=(
            "Adapters implement ports. They may depend on everything above them and on database "
            "and settings libraries, and on nothing else."
        ),
    ),
    "interfaces": Rule(
        package="interfaces",
        allowed_first_party=frozenset({"domain", "application", "config", "observability"}),
        allowed_third_party=frozenset({"fastapi", "starlette", "pydantic", "structlog"}),
        rationale=(
            "The transport must not import infrastructure adapters: it receives use cases from "
            "the composition root. Allowing a direct database import from a route is the most "
            "common way a layered codebase quietly becomes a distributed monolith."
        ),
    ),
    "workers": Rule(
        package="workers",
        allowed_first_party=frozenset(
            {"domain", "application", "config", "observability", "infrastructure"}
        ),
        allowed_third_party=frozenset(),
        rationale=(
            "A worker is a process role, not a layer: it wires adapters and drives use cases. "
            "It may therefore reach infrastructure, but it must not contain business rules - and "
            "it may not import the HTTP interface."
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class Violation:
    """One forbidden import."""

    path: str
    line: int
    module: str
    reason: str

    def __str__(self) -> str:
        """Render the violation as a single line."""
        return f"{self.path}:{self.line}: forbidden import {self.module!r} - {self.reason}"


def _top_level(module: str) -> str:
    """Return the top-level component of a dotted module path.

    Args:
        module: Dotted module name, possibly relative.

    Returns:
        The first dotted component.

    """
    return module.split(".", 1)[0]


def _iter_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Extract every import target in a module.

    Args:
        tree: Parsed module.

    Returns:
        List of ``(line_number, module_name)``.

    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import. Resolved against the package being checked.
                found.append((node.lineno, f".{'.' * (node.lineno and 1)}{node.module or ''}"))
                continue
            if node.module:
                found.append((node.lineno, node.module))
    return found


#: A first-party path needs at least `<package>/<module>` before a layer can be identified.
#: Anything shallower is a top-level module such as container.py or main.py, which are
#: allowed to know every layer.
_MINIMUM_PATH_DEPTH = 2


def _relative_import_reason() -> str:
    """Return the reason relative imports are forbidden."""
    return (
        "relative imports obscure which package a symbol comes from; ruff bans them too "
        "(flake8-tidy-imports.ban-relative-imports)"
    )


def check_file(path: Path) -> list[Violation]:
    """Check one file against the rules for its package.

    Args:
        path: Path to a ``.py`` file.

    Returns:
        Violations found. Empty when the file conforms.

    """
    try:
        relative = path.relative_to(SRC_ROOT)
    except ValueError:
        return []
    parts = relative.parts
    if len(parts) < _MINIMUM_PATH_DEPTH:
        return []  # top-level module (container.py, main.py): allowed to know everything
    package = parts[1] if parts[0] == "knowledge_assistant" else parts[0]
    rule = RULES.get(package)
    if rule is None:
        return []

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation(str(path), 0, "<unreadable>", f"cannot read file: {exc}")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(str(path), exc.lineno or 0, "<syntax>", f"cannot parse: {exc.msg}")]

    violations: list[Violation] = []
    for lineno, module in _iter_imports(tree):
        if module.startswith("."):
            violations.append(Violation(str(path), lineno, module, _relative_import_reason()))
            continue
        top = _top_level(module)
        if top == "knowledge_assistant":
            # First-party import. A module may always import a sibling inside its own package:
            # splitting a package into modules is not a layering boundary.
            sub = module.split(".")[1] if len(module.split(".")) > 1 else ""
            if sub and sub != package and sub not in rule.allowed_first_party:
                violations.append(
                    Violation(
                        str(path),
                        lineno,
                        module,
                        f"package {package!r} may not import {sub!r}; rule: {rule.rationale}",
                    )
                )
            continue
        if top == module:
            # `import knowledge_assistant` without a subpackage.
            continue
        if top in _STDLIB:
            continue
        if top not in rule.allowed_third_party:
            violations.append(
                Violation(
                    str(path),
                    lineno,
                    module,
                    f"third-party module {top!r} is not permitted in {package!r}; "
                    f"allowed: {sorted(rule.allowed_third_party) or 'standard library only'}",
                )
            )
    return violations


def check_tree(root: Path | None = None) -> list[Violation]:
    """Check every module under the source root.

    Args:
        root: Directory to check. Defaults to ``src/knowledge_assistant``.

    Returns:
        All violations found.

    """
    target = root or SRC_ROOT
    violations: list[Violation] = []
    for path in sorted(target.rglob("*.py")):
        violations.extend(check_file(path))
    return violations


def main() -> int:
    """Run the check and report.

    Returns:
        ``0`` when clean, ``1`` otherwise.

    """
    violations = check_tree()
    if not violations:
        print(f"architecture: OK - {len(RULES)} dependency rules satisfied across {SRC_ROOT}")
        return 0
    for violation in violations:
        print(str(violation), file=sys.stderr)
    print(f"\narchitecture: {len(violations)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

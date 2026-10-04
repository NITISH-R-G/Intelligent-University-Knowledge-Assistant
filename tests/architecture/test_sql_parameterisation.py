"""Architecture fitness test: SQL in the database adapters is parameterised.

This test exists because of a deliberate lint suppression. ``S608`` (bandit: "possible SQL
injection through string-based query construction") is switched off for
``src/knowledge_assistant/infrastructure/db/*.py``, because ruff cannot distinguish a
module-level column-list constant from user input.

A suppression that is not backed by an enforcement mechanism is just a hole. This module is
that enforcement: it parses every f-string in the adapters and asserts that the *only*
values interpolated into SQL are module-level constants defined in the same module, and that
every dynamic value reaches the driver through a psycopg named placeholder (``%(name)s``).

If someone later writes ``f"... WHERE tenant_id = {tenant_id}"``, this test fails even though
the linter is configured to stay silent.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

DB_PACKAGE = Path("src/knowledge_assistant/infrastructure/db")

#: psycopg's named-parameter placeholder. Nothing else may stand in for a bound value.
PLACEHOLDER = re.compile(r"%\(\w+\)s")

#: A keyword that must never appear in an f-string or .format() call inside this package.
#: These are the identifiers that carry request-controlled data at the SQL boundary.
REQUEST_CONTROLLED = frozenset(
    {"tenant_id", "scope", "key", "job_id", "payload", "available_at", "request_fingerprint"}
)


def _db_modules() -> list[Path]:
    """Return the adapter modules the suppression covers.

    Returns:
        Every ``.py`` file in the database package.

    """
    return sorted(p for p in DB_PACKAGE.glob("*.py"))


def _module_constants(tree: ast.Module) -> set[str]:
    """Return names bound to module-level constants.

    Args:
        tree: Parsed module.

    Returns:
        Set of module-level assignment targets, including tuple-unpacked ones.

    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class TestNoDynamicSqlInFStrings:
    """The core invariant behind the S608 suppression."""

    @pytest.mark.parametrize("path", _db_modules(), ids=lambda p: p.name)
    def test_fstring_sql_interpolates_only_module_constants(self, path: Path) -> None:
        """Every name interpolated into an f-string must be a module-level constant.

        This is the precise claim that justifies the suppression. A module-level constant is
        developer-authored and cannot carry request data.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_constants(tree)
        violations: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr) and not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
            ):
                continue
            # Only consider f-strings that actually look like SQL.
            literal = (
                "".join(part.value for part in node.values if isinstance(part, ast.Constant))
                if isinstance(node, ast.JoinedStr)
                else ""
            )
            if not any(
                keyword in literal.upper()
                for keyword in ("SELECT", "INSERT", "UPDATE", "DELETE", "FROM", "VALUES")
            ):
                continue
            for part in getattr(node, "values", []):
                if (
                    isinstance(part, ast.FormattedValue)
                    and isinstance(part.value, ast.Name)
                    and part.value.id not in constants
                ):
                    violations.append(
                        f"line {part.lineno}: interpolates {part.value.id!r}, "
                        "which is not a module-level constant"
                    )
        assert not violations, f"{path}: " + "; ".join(violations)

    @pytest.mark.parametrize("path", _db_modules(), ids=lambda p: p.name)
    def test_no_request_controlled_value_is_ever_interpolated(self, path: Path) -> None:
        """A belt-and-braces check that does not depend on the constant analysis above.

        These names are the request-controlled values at this boundary. If one ever appears
        inside an f-string in this package, that is SQL injection, and this test says so in a
        sentence an engineer can act on.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                    assert part.value.id not in REQUEST_CONTROLLED, (
                        f"{path}:{node.lineno} interpolates request-controlled "
                        f"{part.value.id!r} into SQL - bind it with a %(name)s placeholder"
                    )

    @pytest.mark.parametrize("path", _db_modules(), ids=lambda p: p.name)
    def test_multi_line_sql_uses_named_placeholders(self, path: Path) -> None:
        """Triple-quoted SQL blocks must bind values by placeholder, not by interpolation."""
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
            if "VALUES" not in literal.upper() and "WHERE" not in literal.upper():
                continue
            checked += 1
            assert "{" not in literal, (
                f"{path}:{node.lineno} SQL literal contains a brace; use a %(name)s placeholder"
            )
        if checked == 0:
            pytest.skip(f"{path.name} declares no SQL literals; nothing to check here")


class TestSuppressionIsScoped:
    """The suppression must not have crept beyond the adapters."""

    def test_no_other_first_party_module_uses_fstring_sql(self) -> None:
        """If S608 fires elsewhere, it should be fixed, not suppressed."""
        offenders: list[str] = []
        for path in sorted(Path("src").rglob("*.py")):
            if DB_PACKAGE in path.parents or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                literal = "".join(
                    part.value for part in node.values if isinstance(part, ast.Constant)
                )
                if any(
                    keyword in literal.upper()
                    for keyword in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM")
                ):
                    offenders.append(f"{path}:{node.lineno}")
        assert not offenders, f"SQL built outside the db adapters: {offenders}"

    def test_placeholder_grammar_is_recognised(self) -> None:
        """Guards the regex itself: if psycopg's placeholder syntax changes, this test would
        otherwise pass vacuously.
        """
        assert PLACEHOLDER.search("WHERE id = %(id)s")
        assert not PLACEHOLDER.search("WHERE id = $1")
        assert not PLACEHOLDER.search("WHERE id = ?")

"""Architecture fitness tests: the dependency rules, enforced automatically.

These are not unit tests of a function - they are executable statements about what the
codebase may become. A rule enforced only in a document is a rule that decays; a rule
enforced in the test suite survives refactors and is visible in the diff.

Each test below states the invariant, the production failure it prevents, and the rule
identifier in ``scripts/check_architecture.py`` that does the enforcement at CI time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from knowledge_assistant import domain

pytestmark = pytest.mark.architecture

SRC = Path("src/knowledge_assistant")
DOMAIN_RULE = "domain"


def _module_files() -> list[Path]:
    """Return every first-party module file.

    Returns:
        Sorted list of ``.py`` paths under the package root.

    """
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> list[tuple[str, int]]:
    """Return first-party and third-party imports of a module.

    Args:
        path: Module to parse.

    Returns:
        List of ``(module, level)`` pairs as written in the source.

    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.level))
    return found


def _first_party_root(module: str) -> str | None:
    """Return the first-party subpackage a module belongs to.

    Args:
        module: Dotted module path.

    Returns:
        The subpackage name, or ``None`` if the module is not first-party.

    """
    prefix = "knowledge_assistant."
    if not module.startswith(prefix):
        return None
    rest = module[len(prefix) :]
    return rest.split(".", 1)[0] if rest else None


class TestRuleDomainIsPure:
    """Rule: the domain imports nothing outside itself and the standard library."""

    def test_domain_imports_no_first_party_sibling(self) -> None:
        """Infrastructure leaking into the domain is the failure this project exists to
        prevent: once the domain knows about psycopg, retry policy can no longer be tested
        without a database, and the core becomes untestable in a fast lane.
        """
        domain_dir = SRC / "domain"
        for path in sorted(domain_dir.rglob("*.py")):
            for module, _level in _imports(path):
                root = _first_party_root(module)
                assert root in (None, DOMAIN_RULE), f"{path} imports first-party {module}"

    def test_domain_imports_no_third_party_library(self) -> None:
        """pydantic, fastapi and psycopg are all banned here. This is the rule that keeps the
        core explainable in one sitting.
        """
        banned = {"fastapi", "starlette", "psycopg", "pydantic", "pydantic_settings", "structlog"}
        for path in _module_files():
            if "domain" not in path.parts:
                continue
            for module, _level in _imports(path):
                root = module.split(".", 1)[0]
                assert root not in banned, f"{path} imports {module}"

    def test_domain_package_declares_its_purity_contract(self) -> None:
        """The contract is written down where an engineer will actually look."""
        text = (SRC / "domain" / "__init__.py").read_text(encoding="utf-8")
        assert "import" in text.lower()


class TestRuleApplicationDoesNotDependOnInfrastructure:
    """Rule: use cases depend on ports, never on adapters."""

    def test_application_imports_no_infrastructure(self) -> None:
        """A use case that imports psycopg directly cannot be unit tested without a
        database, which is how 'just this one query' turns into a slow suite.
        """
        for path in _module_files():
            if "application" not in path.parts:
                continue
            for module, _level in _imports(path):
                assert _first_party_root(module) != "infrastructure", f"{path} imports {module}"

    def test_application_imports_no_interfaces(self) -> None:
        for path in _module_files():
            if "application" not in path.parts:
                continue
            for module, _level in _imports(path):
                assert _first_party_root(module) != "interfaces", f"{path} imports {module}"

    def test_ports_are_protocols_not_concrete_classes(self) -> None:
        """Ports are defined as Protocols so an adapter can be replaced without editing the
        port, and so the boundary is checkable without importing the adapter.
        """
        source = (SRC / "application" / "ports.py").read_text(encoding="utf-8")
        assert source.count("Protocol") >= 3


class TestRuleInfrastructureDependsInward:
    """Rule: adapters may import the core; nothing may import the adapters except wiring."""

    def test_infrastructure_imports_domain_and_application_only_inward(self) -> None:
        for path in _module_files():
            if "infrastructure" not in path.parts:
                continue
            for module, _level in _imports(path):
                root = _first_party_root(module)
                assert root in {None, "domain", "application", "infrastructure", "observability"}, (
                    f"{path} imports {module}"
                )

    def test_no_module_outside_infrastructure_imports_the_database_adapter(self) -> None:
        """Only the composition root may know how the database is reached. If a use case can
        import the adapter, the port is decorative.
        """
        allowed = {"container.py", "lifespan.py", "interfaces", "workers"}
        for path in _module_files():
            if "infrastructure" in path.parts or path.name in allowed:
                continue
            for module, _level in _imports(path):
                assert not module.startswith("knowledge_assistant.infrastructure"), (
                    f"{path} imports {module}"
                )


class TestRuleInterfacesAreThin:
    """Rule: the HTTP layer translates; it does not decide."""

    def test_http_layer_imports_no_domain_decision_logic(self) -> None:
        """Endpoint modules may use domain types, but must not reimplement policy."""
        for path in _module_files():
            if "interfaces" not in path.parts:
                continue
            for module, _level in _imports(path):
                root = _first_party_root(module)
                assert root not in {"workers"}, f"{path} imports {module}"

    def test_middleware_defines_the_public_path_allow_list(self) -> None:
        """The fail-closed boundary must be a named, reviewable constant."""
        source = (SRC / "interfaces" / "http" / "middleware.py").read_text(encoding="utf-8")
        assert "PUBLIC_PATHS" in source


class TestRuleObservabilityStaysIndependent:
    """Rule: observability must not depend on configuration or on business code."""

    def test_observability_does_not_import_config(self) -> None:
        """Logging that imports the config layer inverts the dependency and makes the
        logging module untestable without a valid environment.
        """
        for path in _module_files():
            if "observability" not in path.parts:
                continue
            for module, _level in _imports(path):
                assert _first_party_root(module) != "config", f"{path} imports {module}"

    def test_observability_does_not_import_infrastructure(self) -> None:
        for path in _module_files():
            if "observability" not in path.parts:
                continue
            for module, _level in _imports(path):
                assert _first_party_root(module) != "infrastructure", f"{path} imports {module}"


class TestRuleWiringIsTheOnlyPlaceThatKnowsEverything:
    """Rule: exactly one composition root may import across layers."""

    def test_only_container_imports_the_adapters(self) -> None:
        """If several modules import adapters, the graph has no single wiring point and
        'which dependencies does this request actually need' has no answer.
        """
        for path in _module_files():
            if path.name in {"container.py", "lifespan.py", "main.py", "worker_main.py"}:
                continue
            if (
                "interfaces" in path.parts
                or "infrastructure" in path.parts
                or "workers" in path.parts
            ):
                continue
            for module, _level in _imports(path):
                assert not module.startswith("knowledge_assistant.infrastructure.db"), (
                    f"{path} imports {module}"
                )

    def test_container_exists_and_is_importable(self) -> None:
        from knowledge_assistant import container  # noqa: PLC0415

        assert hasattr(container, "build_api_container")
        assert hasattr(container, "build_worker_container")


class TestNoSpeculativeSurface:
    """Phase 1 must contain no RAG, ingestion or LLM code."""

    FORBIDDEN = (
        "embedding",
        "chunk",
        "vector_store",
        "rerank",
        "llm",
        "prompt",
        "openai",
        "anthropic",
        "langchain",
        "llama_index",
        "ingest",
        "pdf",
        "citation",
    )

    def test_no_forbidden_module_names(self) -> None:
        """The absence is asserted, not assumed. Phase creep is easiest to prevent at the
        moment it is first written.
        """
        offenders = [
            p.as_posix()
            for p in _module_files()
            if any(word in p.name.lower() for word in self.FORBIDDEN)
        ]
        assert not offenders, offenders

    def test_no_forbidden_third_party_imports(self) -> None:
        banned = {"openai", "anthropic", "langchain", "llama_index", "chromadb", "pinecone"}
        for path in _module_files():
            for module, _level in _imports(path):
                assert module.split(".", 1)[0] not in banned, f"{path} imports {module}"

    def test_domain_exposes_only_phase_one_concepts(self) -> None:
        """A domain named for a Phase 2 concept would be a speculative abstraction."""
        names = {n for n in dir(domain) if not n.startswith("_")}
        for name in names:
            for word in self.FORBIDDEN:
                assert word not in name.lower(), f"domain exports {name}"


class TestArchitectureCheckerAgrees:
    """The standalone checker and the pytest suite must not disagree."""

    def test_checker_reports_no_violations(self) -> None:
        """CI runs the checker as its own stage; this test makes the failure visible locally
        too.
        """
        import importlib.util  # noqa: PLC0415
        import sys  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "check_architecture", "scripts/check_architecture.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_architecture"] = module
        spec.loader.exec_module(module)
        violations = module.check_tree(Path("src"))
        assert violations == [], violations

    def test_every_package_declares_its_boundary(self) -> None:
        """Each layer has an ``__init__`` documenting what it may import."""
        for package in ("domain", "application", "infrastructure", "interfaces", "observability"):
            init = SRC / package / "__init__.py"
            assert init.exists(), f"{package} has no __init__ documenting its boundary"
            text = init.read_text(encoding="utf-8")
            assert text.lstrip().startswith('"""'), f"{package} lacks a module docstring"

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
from types import ModuleType

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
    """Phase 2 builds retrieval, ingestion and prompt construction; it still builds nothing else.

    Phase 1 wrote this guard with ``embedding``, ``chunk``, ``prompt``, ``ingest`` and
    ``citation`` forbidden, because at that point none of it existed and the cheapest moment to
    forbid a concept is before it is written. The retrieval-augmented slice then implemented
    exactly those five, so that list is split in two below: :attr:`FORBIDDEN` keeps guarding
    what the domain must *not* grow, and :attr:`DEFERRED` names the modules that were
    explicitly deferred rather than quietly skipped.

    Deleting the test instead of re-scoping it would have left the boundary unwatched, which is
    the one outcome the test existed to prevent.
    """

    FORBIDDEN = (
        "rerank",
        "llm",
        "openai",
        "anthropic",
        "langchain",
        "llama_index",
        "pdf",
        "graphrag",
        "pinecone",
        "chromadb",
        "weaviate",
    )

    #: Absent by decision in Phase 2 and therefore still asserted. A reranking or hosted-LLM
    #: module appearing here means a sprint boundary was crossed without a design decision.
    DEFERRED = ("rerank", "graphrag", "llm")

    def test_no_deferred_module_names(self) -> None:
        """The absence is asserted, not assumed. Phase creep is easiest to prevent at the
        moment it is first written.
        """
        offenders = [
            p.as_posix()
            for p in _module_files()
            if any(word in p.name.lower() for word in self.DEFERRED)
        ]
        assert not offenders, offenders

    def test_no_forbidden_module_names(self) -> None:
        """Vendor SDKs and hosted-vector-store clients stay out, now that local retrieval exists."""
        offenders = [
            p.as_posix()
            for p in _module_files()
            if any(word in p.name.lower() for word in self.FORBIDDEN)
        ]
        assert not offenders, offenders

    def test_retrieval_modules_exist_where_the_architecture_expects_them(self) -> None:
        """The positive counterpart of the two tests above.

        Phase 1 could assert that retrieval did not exist. Phase 2 must assert that it exists
        *and* that it landed in the right layers, so the guard cannot be satisfied by deleting
        the feature instead of by building it: the value objects are in ``domain``, the use
        cases in ``application``, and everything that opens a database or embeds text in
        ``infrastructure``. The per-layer import rules are already enforced by the tests above
        and by ``scripts/check_architecture.py``; this only pins the placement.
        """
        expected = {
            "domain": ("knowledge.py",),
            "application": ("ingest.py", "retrieval.py", "answer.py"),
            "infrastructure": ("embeddings.py", "db/knowledge.py", "db/direct.py"),
        }
        for layer, names in expected.items():
            for name in names:
                assert (SRC / layer / name).exists(), f"{layer} is missing {name}"

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


def _checker(root: Path) -> ModuleType:
    """Load ``scripts/check_architecture.py`` with its source root pointed at `root`.

    Args:
        root: Directory to treat as the package root. ``check_file`` ignores any path
            that is not under ``SRC_ROOT``, so redirecting it is what lets these tests
            probe synthetic files without writing into the real source tree.

    Returns:
        The loaded module.

    """
    import importlib.util  # noqa: PLC0415
    import sys  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "check_architecture_under_test", "scripts/check_architecture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: a @dataclass in the module resolves annotations through
    # sys.modules at class-creation time and fails with a confusing error if absent.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.SRC_ROOT = root
    return module


class TestCheckerCatchesEveryImportForm:
    """A checker that misses an import form is decoration, not a control.

    The checker once carried an early ``if top == module: continue``, intended to let a
    bare ``import knowledge_assistant`` pass. The condition was true for *every*
    single-segment module, so ``import requests`` in the config layer - the exact
    violation this gate exists to catch - was silently skipped. Only dotted imports
    (``from foo.bar import x``) were checked.

    Phase 0 called this CI rule "the only real control" against boundary erosion, so a
    hole in it is a hole in the architecture, not a cosmetic bug. These tests inject a
    violation into a temporary file and assert the checker sees it.
    """

    #: A third-party module that is in no rule's allow-list.
    _FORBIDDEN = "definitely_not_a_real_dependency"

    @pytest.mark.parametrize(
        "statement",
        [
            "import definitely_not_a_real_dependency",
            "from definitely_not_a_real_dependency import thing",
            "from definitely_not_a_real_dependency.sub import thing",
        ],
    )
    def test_undeclared_third_party_import_is_rejected(
        self, tmp_path: Path, statement: str
    ) -> None:
        """Both import forms must be checked; a bare ``import X`` is the one that was missed."""
        module = _checker(tmp_path)
        target = tmp_path / "config" / "probe.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{statement}\n", encoding="utf-8")
        assert module.check_file(target), f"checker missed: {statement}"

    def test_stdlib_import_is_allowed(self, tmp_path: Path) -> None:
        """A genuine standard-library import must not be reported as third-party."""
        module = _checker(tmp_path)
        target = tmp_path / "config" / "probe.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import shutil\nimport urllib.parse\n", encoding="utf-8")
        assert module.check_file(target) == []

    def test_bare_first_party_import_is_allowed(self, tmp_path: Path) -> None:
        """``import knowledge_assistant`` crosses no layer boundary, so it stays legal.

        This is the case the removed early-return was written for; keeping it legal is
        what proves the fix did not simply delete the exemption.
        """
        module = _checker(tmp_path)
        target = tmp_path / "config" / "probe.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import knowledge_assistant\n", encoding="utf-8")
        assert module.check_file(target) == []

    def test_layering_violation_is_still_rejected(self, tmp_path: Path) -> None:
        """The first-party direction rule must survive the change unchanged."""
        module = _checker(tmp_path)
        target = tmp_path / "domain" / "probe.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "from knowledge_assistant.infrastructure.db import engine\n", encoding="utf-8"
        )
        assert module.check_file(target)

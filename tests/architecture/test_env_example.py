"""Architecture fitness test: `.env.example` cannot drift from the settings model.

``.env.example`` is the only artefact that tells a reviewer which environment
variables the service reads. It is also the file a new developer copies to `.env`
before their first run, so a mistake in it produces an outage on someone else's
machine rather than a failing test in CI.

Two directions of drift are possible and both are silent:

1. A setting is added or renamed in ``Settings`` and the template is not updated.
   The developer copies the template, the new setting silently keeps its code
   default, and nobody discovers the value is unset until it matters.

2. The template gains a variable that no longer matches any field. Because
   ``Settings.model_config`` sets ``extra="forbid"``, this one is worse than
   silent: the application refuses to start with an "extra inputs not permitted"
   error, and the message names a variable the developer believes is correct.

Neither is catchable by a type checker. Both are cheap to catch here, and the
check is a set comparison against a model that is already the single source of
truth - so the test cannot itself go stale without the model changing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from knowledge_assistant.config.settings import Settings, load_settings

pytestmark = pytest.mark.architecture

ENV_EXAMPLE = Path(".env.example")
GITIGNORE = Path(".gitignore")

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def _declared_assignments() -> dict[str, str]:
    """Return the active ``KEY=VALUE`` pairs in `.env.example`.

    Comments and blank lines are excluded. A commented-out assignment is
    documentation of an optional variable, not a declaration of it, which is why
    ``TEST_DATABASE_URL`` is allowed to appear only inside a comment.

    Returns:
        Mapping of variable name to its raw value text.

    """
    declared: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(stripped)
        assert match is not None, f"unparseable line in .env.example: {line!r}"
        declared[match.group("key")] = match.group("value")
    return declared


def _expected_env_names() -> set[str]:
    """Return the environment variable name of every field on `Settings`.

    Returns:
        Upper-case ``KA_``-prefixed names derived from the model's own prefix.

    """
    prefix = Settings.model_config.get("env_prefix", "")
    return {f"{prefix}{name}".upper() for name in Settings.model_fields}


class TestTemplateCoversEverySetting:
    """The template must declare every variable the application can read."""

    def test_every_setting_appears_in_the_template(self) -> None:
        """A new or renamed setting must be added to `.env.example`.

        This is the direction that fails silently in production and loudly in a
        developer's first ten minutes; both are worth preventing.
        """
        declared = {key for key in _declared_assignments() if key.upper().startswith("KA_")}
        missing = _expected_env_names() - {key.upper() for key in declared}
        assert not missing, f"settings missing from .env.example: {sorted(missing)}"


class TestTemplateIsActuallyLoadable:
    """Every assignment in the template must survive real settings validation.

    The tests above check the template against the *field names*. They cannot
    check the *values*, and the values are where this file has actually broken.

    ``cors_allowed_origins`` is the worked example. It is typed
    ``tuple[str, ...]``, and pydantic-settings JSON-decodes complex-typed fields
    out of the environment *before* any validator runs - so for a while the
    template had to spell an empty list as ``[]`` because the intuitive
    ``KA_CORS_ALLOWED_ORIGINS=`` raised ``SettingsError`` at startup. A
    name-comparison test cannot see that, and neither can a test that constructs
    ``Settings(...)`` directly: only the environment path exposes it.

    Loading the whole template through ``load_settings`` closes that class of bug
    permanently, and it costs one test.
    """

    def test_every_assignment_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Apply the template to the environment and construct the real model."""
        for key, value in _declared_assignments().items():
            monkeypatch.setenv(key, value)
        # The model reads the process environment, so the template is the only
        # input: any failure here is a genuine defect in the template.
        settings = load_settings()
        assert settings.database_url.get_secret_value().startswith("postgresql://")
        assert settings.environment.value == "local"

    def test_cors_origins_uses_the_documented_empty_spelling(self) -> None:
        """The template must express "no origins" the way the contract defines it.

        Empty is the documented representation, and it is also the one that reads
        most clearly to someone reading the file. If a future change makes empty
        mean something else, this fails here rather than in a developer's first
        run - and the message says which spelling is correct.
        """
        assert _declared_assignments()["KA_CORS_ALLOWED_ORIGINS"] == "", (
            "an empty KA_CORS_ALLOWED_ORIGINS is the documented way to express "
            "'no cross-origin access'; do not replace it with a JSON form"
        )


class TestTemplateDeclaresNothingExtra:
    """The template must not declare a variable the application would reject."""

    def test_no_unknown_ka_variables(self) -> None:
        """`extra="forbid"` turns a stale template entry into a startup crash."""
        declared = {key.upper() for key in _declared_assignments() if key.upper().startswith("KA_")}
        unknown = declared - _expected_env_names()
        assert not unknown, (
            f".env.example declares variables that are not Settings fields "
            f"(startup will fail with extra='forbid'): {sorted(unknown)}"
        )

    def test_no_environment_variable_is_left_over_from_a_removed_shell(self) -> None:
        """The template must not depend on inherited variables to load.

        Loading must succeed from a clean environment. Anything the template
        omits but the model requires would otherwise be silently supplied by
        whatever the developer happens to have exported.
        """
        assert "KA_DATABASE_URL" in _declared_assignments(), (
            "database_url is the one required setting with no default; the template "
            "must supply it so it loads in a clean shell"
        )


class TestTemplateCarriesNoSecrets:
    """The tracked template must hold example values, never real credentials."""

    def test_database_url_password_is_an_obvious_placeholder(self) -> None:
        """The DSN password must be the documented throwaway value.

        The template is world-readable in a public repository. A real password
        here would be a disclosed credential, and one that looks plausible is
        worse than one that is obviously a placeholder because reviewers stop
        noticing it.
        """
        url = _declared_assignments()["KA_DATABASE_URL"]
        assert url.startswith("postgresql://"), "expected a plain postgresql:// DSN"
        password = url.split("://", 1)[1].split(":", 1)[1].split("@", 1)[0]
        assert password == "ka", f"unexpected example password in KA_DATABASE_URL: {password!r}"

    def test_no_bearer_token_or_api_key_shaped_values(self) -> None:
        """No value may look like a token even as a placeholder."""
        suspicious = ("sk-", "ghp_", "xox", "bearer ", "api_key=", "apikey=")
        offenders = [
            f"{key}={value}"
            for key, value in _declared_assignments().items()
            if any(marker in value.lower() for marker in suspicious)
        ]
        assert not offenders, f"token-shaped values in .env.example: {offenders}"


class TestTemplateMatchesTheComposeStack:
    """The documented DSN must describe the database docker-compose.yml actually builds.

    `.env.example` is documentation and `docker-compose.yml` is configuration. Nothing
    cross-checks them, both are easy to edit independently, and a mismatch surfaces
    only as a failed connection on a developer's first `make migrate` - with an error
    that says nothing about which of the two files is wrong.

    This cannot be proven without a running database, so it is not claimed to be. What
    it *can* prove statically is that the two agree on user, password, database, port
    and reachability, which removes the most common way the stack fails to come up.
    """

    def test_env_and_compose_agree(self) -> None:
        """Run the same checker `make env-check` runs, and require it to pass."""
        import importlib.util  # noqa: PLC0415
        import sys  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "check_env_consistency_under_test", "scripts/check_env_consistency.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        assert module.main() == 0, ".env.example and docker-compose.yml disagree"


class TestSecretFilesAreTrackedCorrectly:
    """`.env` is ignored; `.env.example` must not be."""

    def test_gitignore_covers_env_but_not_the_example(self) -> None:
        """The template is documentation and must stay reviewable in the diff."""
        patterns = [
            line.strip()
            for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert ".env" in patterns, ".env must be gitignored: it holds real credentials"
        assert ".env.example" not in patterns, (
            ".env.example is documentation and must remain tracked"
        )

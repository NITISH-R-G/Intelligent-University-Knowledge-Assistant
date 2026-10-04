"""Unit tests for configuration validation, classification and leak prevention.

Configuration is the first thing that runs in every process, which makes it both the cheapest
place to fail fast and the easiest place to leak a credential. Three guarantees are pinned
here: invalid configuration is rejected at startup rather than at first use, every setting is
classified, and no classified value reaches a log or a public endpoint.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge_assistant.config.categories import CATEGORY_RULES, MASK, ConfigCategory, mask_value
from knowledge_assistant.config.settings import (
    SETTING_CATEGORIES,
    Settings,
    _assert_every_setting_classified,
    load_settings,
)

pytestmark = pytest.mark.unit

DSN = "postgresql://app:dev@localhost:5432/ka"


def _settings(**overrides: object) -> Settings:
    """Build settings with a valid baseline.

    Args:
        **overrides: Field overrides.

    Returns:
        A ``Settings`` instance.

    """
    base: dict[str, object] = {"environment": "local", "database_url": DSN}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestClassificationIsTotal:
    """An unclassified setting is a setting nobody knows how to log."""

    def test_every_field_is_classified(self) -> None:
        """Adding a field without a category must fail, loudly, at import time."""
        _assert_every_setting_classified(Settings)

    def test_classification_map_has_no_extras(self) -> None:
        """A category for a field that no longer exists is stale documentation."""
        actual = set(Settings.model_fields)
        assert set(SETTING_CATEGORIES) == actual

    def test_all_four_categories_are_used(self) -> None:
        """The four-category scheme is documented; a category with no members means the
        documentation describes a distinction the code does not make.
        """
        used = set(SETTING_CATEGORIES.values())
        assert used == set(ConfigCategory)

    def test_database_url_is_a_secret(self) -> None:
        """The DSN embeds a password. It is the single most leaked value in a service."""
        assert SETTING_CATEGORIES["database_url"] is ConfigCategory.SECRET


class TestValidation:
    """Fail-fast validation: a bad configuration must stop the process at startup."""

    def test_valid_settings_load(self) -> None:
        assert _settings().port == 8080

    def test_unknown_setting_is_rejected(self) -> None:
        """A typo'd environment variable must not be silently ignored. ``extra="forbid"``
        turns "works locally, ignored in CI" into a startup crash.
        """
        with pytest.raises(ValidationError):
            _settings(totally_made_up_setting=1)

    def test_pool_max_below_min_is_rejected(self) -> None:
        """A maximum below the minimum is an impossible pool; psycopg would fail later,
        at connection time, in production.
        """
        with pytest.raises(ValidationError, match="db_pool_max_size must be >= db_pool_min_size"):
            _settings(db_pool_min_size=10, db_pool_max_size=5)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"port": 0},
            {"port": 70000},
            {"db_statement_timeout_ms": 0},
            {"max_request_body_bytes": 10},
            {"worker_max_attempts": 99},
            {"worker_lease_seconds": 1},
            {"log_level": "trace"},
        ],
    )
    def test_out_of_range_values_are_rejected(self, overrides: dict[str, object]) -> None:
        """Bounds exist so that a typo cannot disable a safety limit."""
        with pytest.raises(ValidationError):
            _settings(**overrides)

    def test_missing_database_url_is_rejected(self) -> None:
        """There is no safe default DSN. Guessing one would mean connecting to whatever
        happens to be on localhost:5432.
        """
        with pytest.raises(ValidationError):
            Settings(environment="local")  # type: ignore[call-arg]

    def test_cors_origins_accept_comma_separated_string(self) -> None:
        """Container environments inject lists as strings; rejecting that would push people
        toward JSON files for configuration.
        """
        assert _settings(
            cors_allowed_origins="https://a.example, https://b.example"
        ).cors_allowed_origins == ("https://a.example", "https://b.example")

    def test_settings_are_frozen(self) -> None:
        """Settings are process-wide state; mutation at runtime means two components
        disagreed about the configuration.
        """
        with pytest.raises(ValidationError):
            _settings().port = 9090  # type: ignore[misc]


class TestProductionSafety:
    """Staging and production refuse postures that are merely convenient locally."""

    def _prod(self, **overrides: object) -> Settings:
        base: dict[str, object] = {
            "environment": "production",
            "host": "0.0.0.0",
            "database_url": DSN,
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    def test_valid_production_config_loads(self) -> None:
        assert self._prod().environment.value == "production"

    def test_loopback_host_rejected_in_production(self) -> None:
        """Binding to 127.0.0.1 in production produces a service that is healthy and
        unreachable - the worst possible failure, because every check passes.
        """
        with pytest.raises(ValidationError, match="must not be loopback"):
            self._prod(host="127.0.0.1")

    def test_disabling_authentication_rejected_in_production(self) -> None:
        """Phase 1 has no auth implementation, so the only thing preventing unauthenticated
        business endpoints is this flag.
        """
        with pytest.raises(ValidationError, match="require_authentication must be true"):
            self._prod(require_authentication=False)

    def test_wildcard_cors_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="cors_allowed_origins"):
            self._prod(cors_allowed_origins=("*",))

    def test_debug_logging_rejected_in_production(self) -> None:
        """Debug logging in production is both a cost and a disclosure risk: it routinely
        logs request payloads.
        """
        with pytest.raises(ValidationError, match="log_level"):
            self._prod(log_level="debug")

    def test_non_loopback_metrics_rejected_in_production(self) -> None:
        """Metrics expose dependency names, error rates and queue depth to anyone who can
        reach the port. That is an information-disclosure surface, not a convenience.
        """
        with pytest.raises(ValidationError, match="metrics_host must be loopback"):
            self._prod(metrics_host="0.0.0.0")

    def test_local_environment_permits_loopback(self) -> None:
        """Local development must not be forced to look like production."""
        assert _settings(host="127.0.0.1").host == "127.0.0.1"

    def test_all_production_problems_are_reported_at_once(self) -> None:
        """Reporting one problem per restart turns a misconfigured deploy into a guessing
        game; the operator should see the full list immediately.
        """
        with pytest.raises(ValidationError) as excinfo:
            self._prod(
                host="127.0.0.1",
                require_authentication=False,
                log_level="debug",
                metrics_host="0.0.0.0",
                cors_allowed_origins=("*",),
            )
        message = str(excinfo.value)
        for fragment in ("loopback", "require_authentication", "cors_allowed_origins", "log_level"):
            assert fragment in message


class TestNoLeaks:
    """The values most likely to leak, asserted not to."""

    def test_safe_dump_masks_the_dsn(self) -> None:
        dump = _settings().safe_dump()
        assert dump["database_url"] == MASK
        assert "dev" not in str(dump)

    def test_safe_dump_masks_security_sensitive_values(self) -> None:
        dump = _settings(host="0.0.0.0").safe_dump()
        assert dump["host"] == MASK

    def test_public_summary_is_an_allow_list(self) -> None:
        """The public endpoint must not be a filtered dump - a filter leaks whatever a
        future commit forgets to classify.
        """
        summary = _settings().public_summary()
        assert set(summary) == {"service", "environment"}
        assert "database_url" not in summary

    def test_public_summary_contains_no_secret_substring(self) -> None:
        rendered = str(_settings().public_summary())
        for secret in ("postgres", "password", "://"):
            assert secret not in rendered

    def test_repr_does_not_expose_the_dsn(self) -> None:
        """``repr`` reaches tracebacks and debugger output; a SecretStr field must not be
        revealed there.
        """
        assert "dev@localhost" not in repr(_settings())

    @pytest.mark.parametrize("category", list(ConfigCategory))
    def test_mask_rule_matches_category_rules(self, category: ConfigCategory) -> None:
        """The mask helper and the documented rule table must agree."""
        masked = mask_value(category, "value")
        expected = MASK if CATEGORY_RULES[category]["masked_in_dumps"] else "value"
        assert masked == expected

    def test_public_and_non_sensitive_are_unmasked(self) -> None:
        """Over-masking is also a defect: an operator cannot debug a masked host."""
        assert mask_value(ConfigCategory.NON_SENSITIVE, "8080") == "8080"
        assert mask_value(ConfigCategory.PUBLIC, "v1") == "v1"


class TestLoading:
    """Environment loading, the only supported source."""

    def test_load_settings_reads_prefixed_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KA_DATABASE_URL", "postgresql://x:y@db:5432/ka")
        monkeypatch.setenv("KA_PORT", "9999")
        assert load_settings().port == 9999

    def test_load_settings_rejects_invalid_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad value in the environment must raise, so the entrypoint can exit 2 with a
        clear message rather than starting in a half-configured state.
        """
        monkeypatch.setenv("KA_DATABASE_URL", "postgresql://x:y@db:5432/ka")
        monkeypatch.setenv("KA_PORT", "not-a-number")
        with pytest.raises(ValidationError):
            load_settings()


class TestCorsOriginsFromEnvironment:
    """The `KA_CORS_ALLOWED_ORIGINS` contract, exercised through the real environment.

    These tests exist because the equivalent kwarg-based test passed while the
    documented contract was unreachable. pydantic-settings JSON-decodes complex-typed
    fields out of the environment *before* any validator runs, so `Settings(
    cors_allowed_origins="a,b")` validated happily while `KA_CORS_ALLOWED_ORIGINS=a,b`
    raised `SettingsError` in the settings source and the validator never executed.

    Every test here goes through `load_settings()` rather than the constructor for that
    reason. A test of the constructor cannot see this class of bug.
    """

    @pytest.fixture(autouse=True)
    def _database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`database_url` is the one setting with no default; supply it for every case."""
        monkeypatch.setenv("KA_DATABASE_URL", DSN)

    def _origins(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> tuple[str, ...]:
        """Load settings with the variable set to `raw` and return the parsed origins.

        Args:
            monkeypatch: Pytest environment patcher.
            raw: Raw environment value.

        Returns:
            The validated origin tuple.

        """
        monkeypatch.setenv("KA_CORS_ALLOWED_ORIGINS", raw)
        return load_settings().cors_allowed_origins

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_empty_value_means_no_cross_origin_access(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Empty must be well-defined, not an error.

        Unset and empty are the same intent, and `KA_CORS_ALLOWED_ORIGINS=` is the
        spelling people actually write. Failing here would push an operator to invent a
        dummy origin rather than to mean "none".
        """
        assert self._origins(monkeypatch, raw) == ()

    def test_single_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One origin, no comma: the common case for a single-origin deployment."""
        assert self._origins(monkeypatch, "https://a.example") == ("https://a.example",)

    def test_comma_separated_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The documented contract: what a Docker env var or shell export naturally yields."""
        assert self._origins(monkeypatch, "https://a.example,https://b.example") == (
            "https://a.example",
            "https://b.example",
        )

    def test_whitespace_is_stripped_and_blank_entries_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace is cosmetic, not semantic.

        Tolerating it stops a trailing comma in a YAML list or a space after each comma
        from being a startup failure. Embedded whitespace inside an entry is still
        rejected - see `test_malformed_input_is_rejected`.
        """
        assert self._origins(monkeypatch, "  https://a.example , https://b.example  ,  ") == (
            "https://a.example",
            "https://b.example",
        )

    def test_json_array_still_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The JSON form keeps working; it is what pydantic-settings requires elsewhere.

        Removing it would break anyone who set the variable this way and succeeded,
        which is not a change this fix is entitled to make.
        """
        assert self._origins(monkeypatch, '["https://a.example", "https://b.example"]') == (
            "https://a.example",
            "https://b.example",
        )

    def test_json_empty_array_means_no_cross_origin_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`[]` and empty string must agree, or the same intent has two meanings."""
        assert self._origins(monkeypatch, "[]") == ()

    def test_origin_with_port_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dev frontend runs on a non-default port; rejecting it would be a false negative."""
        assert self._origins(monkeypatch, "http://localhost:3000") == ("http://localhost:3000",)

    @pytest.mark.parametrize(
        "raw",
        [
            "a.example",  # bare hostname: not an origin
            "//a.example",  # missing scheme
            "file://a.example",  # not an http(s) scheme
            "https://a.example/",  # trailing slash - the browser never sends one
            "https://a.example/admin",  # a path is not part of an Origin header
            "https://a.example?q=1",
            "http://a.exa mple",  # embedded whitespace
            "[unclosed",  # malformed JSON
            '"https://a.example"',  # JSON string, not array
            "[1, 2]",  # JSON array of non-strings
        ],
    )
    def test_malformed_input_is_rejected(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """Invalid origins must fail loudly rather than becoming a permissive list.

        `cors_allowed_origins` is SECURITY_SENSITIVE: silently keeping `a.example` would
        leave an operator believing cross-origin access is restricted to a host it is not
        restricted to. Failing at startup is the recoverable outcome.
        """
        monkeypatch.setenv("KA_CORS_ALLOWED_ORIGINS", raw)
        with pytest.raises(ValidationError):
            load_settings()

    def test_wildcard_is_accepted_here_but_refused_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`*` is a real CORS value, so field validation must not claim otherwise.

        It stays valid here and invalid in production, which is the existing policy: the
        field-level rule accepts the value, the production-safety rule refuses it. If this
        test ever fails because `*` was rejected at field level, the production guard has
        become unreachable and would silently stop protecting anything.
        """
        assert self._origins(monkeypatch, "*") == ("*",)
        monkeypatch.setenv("KA_ENVIRONMENT", "production")
        monkeypatch.setenv("KA_HOST", "0.0.0.0")
        monkeypatch.setenv("KA_METRICS_HOST", "0.0.0.0")
        monkeypatch.setenv("KA_REQUIRE_AUTHENTICATION", "true")
        with pytest.raises(ValidationError, match="cors_allowed_origins"):
            load_settings()

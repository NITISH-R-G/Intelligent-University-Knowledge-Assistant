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

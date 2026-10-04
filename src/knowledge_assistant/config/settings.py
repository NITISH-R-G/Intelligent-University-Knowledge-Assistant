"""Typed settings, loaded from the environment and validated at startup.

Design rules, each of which exists because of a specific operational failure:

**Fail fast, fail loudly, fail before accepting traffic.** Validation runs at process start. A
service that starts with an invalid configuration and discovers it on the first request is a
service that fails *after* a load balancer has routed traffic to it.

**Secrets are typed as secrets.** ``SecretStr`` from pydantic makes accidental serialisation a
type error rather than a logging mistake. The repr of a ``SecretStr`` is already masked, which is
why settings objects are safe to interpolate into an exception message.

**Cross-field rules are validated, not documented.** "TLS must be on in production" and "the
database host must not be the loopback interface in production" are rules that, unenforced,
produce an outage or a finding in a penetration test. ``model_validator`` turns them into a
startup failure.

**No default is a secret.** Every secret has no default, so a missing secret is a startup
error rather than a well-known development credential.

``DATABASE_URL`` uses ``pydantic.SecretStr`` because a Postgres URL may contain a password. That
makes it impossible to print the settings object and leak the database credential.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_assistant.config.categories import ConfigCategory, mask_value

__all__ = [
    "Environment",
    "LogFormat",
    "Settings",
    "load_settings",
    "SETTING_CATEGORIES",
]

_ENV_PREFIX: Final[str] = "KA_"

#: Hosts that mean "this machine only". Used by the production-safety validator, which must
#: refuse a configuration that binds either the service or its metrics to loopback.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})


class Environment(enum.StrEnum):
    """Deployment environment.

    ``LOCAL`` and ``TEST`` are deliberately separate: a test run must be able to assert
    production-safe behaviour without pretending to be local, and vice versa.
    """

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        """Return whether this environment must apply production safety rules."""
        return self in (Environment.STAGING, Environment.PRODUCTION)


class LogFormat(enum.StrEnum):
    """Log encoding."""

    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """Fully validated application settings.

    Populated from environment variables prefixed ``KA_``. No settings file is read: a file
    introduces a second source of truth whose precedence over the environment is a source of
    "works locally, broken in CI" incidents.
    """

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        extra="forbid",  # a typo'd variable is an error, not a silently ignored setting
        frozen=True,  # settings are process-wide state; mutation is a bug
        case_sensitive=False,
    )

    # --- identity / process -------------------------------------------------
    environment: Annotated[Environment, Field(description="Deployment environment")] = (
        Environment.LOCAL
    )
    service_name: Annotated[str, Field(min_length=1, max_length=64)] = "knowledge-assistant"

    # --- HTTP ---------------------------------------------------------------
    host: Annotated[str, Field(min_length=1)] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    #: Requests larger than this are refused before parsing. Unbounded bodies are a
    #: denial-of-service vector; the limit is a policy, documented in the API docs.
    max_request_body_bytes: Annotated[int, Field(ge=1024, le=100 * 1024 * 1024)] = 1 * 1024 * 1024

    # --- database (SECURITY_SENSITIVE: SecretStr because it may embed a password) ---
    database_url: Annotated[
        SecretStr,
        Field(description="PostgreSQL DSN; SecretStr so it cannot be printed"),
    ]
    db_pool_min_size: Annotated[int, Field(ge=1, le=64)] = 2
    db_pool_max_size: Annotated[int, Field(ge=1, le=512)] = 10
    #: Statement timeout. PostgreSQL's default is infinite, which turns one bad query into a
    #: pool exhaustion outage. Set deliberately and low.
    db_statement_timeout_ms: Annotated[int, Field(ge=100, le=600_000)] = 5_000
    db_connect_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 5

    # --- worker -------------------------------------------------------------
    worker_poll_interval_seconds: Annotated[float, Field(ge=0.01, le=60.0)] = 1.0
    worker_lease_seconds: Annotated[int, Field(ge=5, le=3600)] = 30
    worker_max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    worker_backoff_base_seconds: Annotated[float, Field(gt=0, le=300)] = 1.0
    worker_backoff_cap_seconds: Annotated[float, Field(gt=0, le=86400)] = 300.0

    # --- health -------------------------------------------------------------
    health_probe_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 2.0

    # --- idempotency --------------------------------------------------------
    idempotency_retention_seconds: Annotated[int, Field(ge=60, le=7 * 24 * 3600)] = 86_400

    # --- observability ------------------------------------------------------
    log_level: Annotated[Literal["debug", "info", "warning", "error", "critical"], Field()] = "info"
    log_format: Annotated[LogFormat, Field()] = LogFormat.JSON
    metrics_enabled: Annotated[bool, Field()] = True
    #: Metrics port is separate from the API port so that metrics can be bound to loopback
    #: only, which is the default posture: scraping is an internal operation.
    metrics_host: Annotated[str, Field(min_length=1)] = "127.0.0.1"
    metrics_port: Annotated[int, Field(ge=1, le=65535)] = 9100

    # --- security -----------------------------------------------------------
    #: Phase 1 has no authentication implementation. This flag makes that explicit rather
    #: than implicit: it defaults to True so a deployment cannot accidentally expose
    #: unauthenticated *business* endpoints before Phase 2 implements auth. See
    #: docs/08-security/AUTHN_BOUNDARY.md.
    require_authentication: Annotated[bool, Field()] = True
    cors_allowed_origins: Annotated[tuple[str, ...], Field()] = ()

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept a comma-separated string so the variable is convenient in a container env.

        Args:
            value: Raw value from the environment.

        Returns:
            A list of origins, or the value unchanged.

        """
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("db_pool_max_size")
    @classmethod
    def _pool_max_at_least_min(cls, value: int, info: Any) -> int:
        """Reject a pool whose maximum is below its minimum.

        Args:
            value: Candidate maximum.
            info: Validation context carrying previously validated fields.

        Returns:
            The validated maximum.

        Raises:
            ValueError: If the maximum is smaller than the minimum.

        """
        minimum = info.data.get("db_pool_min_size")
        if isinstance(minimum, int) and value < minimum:
            msg = "db_pool_max_size must be >= db_pool_min_size"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Self:
        """Apply rules that only make sense outside local development.

        Returns:
            The validated settings.

        Raises:
            ValueError: If a production safety rule is violated.

        """
        if not self.environment.is_production_like:
            return self
        problems: list[str] = []
        if self.host in _LOOPBACK_HOSTS:
            problems.append(
                "host must not be loopback in staging/production: the service would be unreachable"
            )
        if not self.require_authentication:
            problems.append("require_authentication must be true in staging/production")
        if "*" in self.cors_allowed_origins:
            problems.append("cors_allowed_origins must not contain '*' in staging/production")
        if self.log_level == "debug":
            problems.append("log_level must not be 'debug' in staging/production")
        if self.metrics_host not in _LOOPBACK_HOSTS:
            problems.append(
                "metrics_host must be loopback in staging/production unless a scrape proxy is "
                "configured; metrics expose internals"
            )
        if problems:
            msg = "invalid production configuration: " + "; ".join(problems)
            raise ValueError(msg)
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Return a settings dump with SECRET and SECURITY_SENSITIVE values masked.

        Returns:
            Mapping of setting name to value or ``"***"``. Safe to log and to attach to a
            support ticket.

        """
        raw = self.model_dump(mode="python")
        # The category, not the value's Python type, decides masking. A SecretStr field
        # survives model_dump as a SecretStr object, so a type check would skip exactly the
        # value this function exists to protect.
        return {
            key: mask_value(category, raw.get(key)) for key, category in SETTING_CATEGORIES.items()
        }

    def public_summary(self) -> dict[str, Any]:
        """Return the small set of values safe to expose on a public endpoint.

        Deliberately an explicit allow-list rather than a filtered version of the whole dump:
        an allow-list cannot leak a setting added later without someone editing this function.
        """
        return {
            "service": self.service_name,
            "environment": self.environment.value,
        }


#: Classification of every setting. Kept next to the model so adding a field without
#: classifying it is visible in review.
SETTING_CATEGORIES: Final[dict[str, ConfigCategory]] = {
    # service_name and environment are exactly what /version returns to an unauthenticated
    # caller, so they are PUBLIC by the category's own definition rather than merely
    # "not secret".
    "environment": ConfigCategory.PUBLIC,
    "service_name": ConfigCategory.PUBLIC,
    "host": ConfigCategory.SECURITY_SENSITIVE,
    "port": ConfigCategory.NON_SENSITIVE,
    "max_request_body_bytes": ConfigCategory.NON_SENSITIVE,
    "database_url": ConfigCategory.SECRET,
    "db_pool_min_size": ConfigCategory.NON_SENSITIVE,
    "db_pool_max_size": ConfigCategory.NON_SENSITIVE,
    "db_statement_timeout_ms": ConfigCategory.NON_SENSITIVE,
    "db_connect_timeout_seconds": ConfigCategory.NON_SENSITIVE,
    "worker_poll_interval_seconds": ConfigCategory.NON_SENSITIVE,
    "worker_lease_seconds": ConfigCategory.NON_SENSITIVE,
    "worker_max_attempts": ConfigCategory.NON_SENSITIVE,
    "worker_backoff_base_seconds": ConfigCategory.NON_SENSITIVE,
    "worker_backoff_cap_seconds": ConfigCategory.NON_SENSITIVE,
    "health_probe_timeout_seconds": ConfigCategory.NON_SENSITIVE,
    "idempotency_retention_seconds": ConfigCategory.NON_SENSITIVE,
    "log_level": ConfigCategory.NON_SENSITIVE,
    "log_format": ConfigCategory.NON_SENSITIVE,
    "metrics_enabled": ConfigCategory.NON_SENSITIVE,
    "metrics_host": ConfigCategory.SECURITY_SENSITIVE,
    "metrics_port": ConfigCategory.NON_SENSITIVE,
    "require_authentication": ConfigCategory.NON_SENSITIVE,
    "cors_allowed_origins": ConfigCategory.SECURITY_SENSITIVE,
}


def _assert_every_setting_classified(settings_cls: type[Settings]) -> None:
    """Assert that every field on ``Settings`` appears in ``SETTING_CATEGORIES``.

    Raises:
        AssertionError: If a field is unclassified. Called at import time so an unclassified
            setting cannot ship.

    """
    fields = set(settings_cls.model_fields)
    classified = set(SETTING_CATEGORIES)
    missing = fields - classified
    extra = classified - fields
    assert not missing, f"unclassified settings: {sorted(missing)}"  # noqa: S101
    assert not extra, f"classified but non-existent settings: {sorted(extra)}"  # noqa: S101


_assert_every_setting_classified(Settings)


def load_settings(**overrides: Any) -> Settings:
    """Load and validate settings from the environment.

    Args:
        **overrides: Explicit field overrides, used by tests. Environment variables remain the
            primary source; overrides are applied last.

    Returns:
        Validated settings.

    Raises:
        pydantic.ValidationError: If any setting is invalid or a required secret is missing.
            The message names the field and never echoes a secret value.

    """
    return Settings(**overrides)  # type: ignore[call-arg]

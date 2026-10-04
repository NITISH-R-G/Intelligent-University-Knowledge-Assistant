"""Unit tests for the error taxonomy.

These tests assert the *properties* that make the taxonomy trustworthy - totality, closedness and
non-leakage - rather than restating the table. A test that mirrors the implementation is a test
that cannot fail when the implementation is wrong.
"""

from __future__ import annotations

import pytest

from knowledge_assistant.domain import errors
from knowledge_assistant.domain.errors import (
    ApplicationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    DependencyUnavailableError,
    ErrorKind,
    ErrorPolicy,
    InternalError,
    NotFoundError,
    RateLimitedError,
    TimeoutError_,
    ValidationError,
)

pytestmark = pytest.mark.unit


def test_every_kind_has_a_complete_policy() -> None:
    """Every error kind must have a full policy entry; the mapping is total."""
    for kind in ErrorKind:
        policy = errors.POLICY_BY_KIND[kind]
        assert set(policy) == {
            "http_status",
            "retryable",
            "log_level",
            "policy",
            "client_message",
        }
        assert 100 <= int(policy["http_status"]) < 600
        assert isinstance(policy["retryable"], bool)
        assert policy["log_level"] in {"debug", "info", "warning", "error", "critical"}
        assert isinstance(policy["policy"], ErrorPolicy)
        assert isinstance(policy["client_message"], str) and policy["client_message"]


def test_every_kind_has_context_allowlist() -> None:
    """Every kind must declare which context keys may cross the trust boundary."""
    for kind in ErrorKind:
        assert kind in errors._ALLOWED_CONTEXT_KEYS, f"{kind} has no context allow-list"


def test_internal_and_config_errors_expose_no_context() -> None:
    """The two kinds most likely to carry sensitive data must expose nothing."""
    for kind in (ErrorKind.INTERNAL, ErrorKind.CONFIGURATION):
        error = ApplicationError("boom", detail={"dsn": "postgres://u:p@h/db", "secret": "x"})
        error.kind = kind  # type: ignore[misc]
        assert error.public_context() == {}


def test_context_is_filtered_by_allowlist() -> None:
    """Non-allow-listed context keys must be dropped, not merely unused."""
    error = DependencyUnavailableError(
        "db down",
        dependency="postgres",
        detail={"connection_string": "postgres://user:pw@host/db", "attempt": 3},
    )
    exposed = error.public_context()
    assert exposed == {"dependency": "postgres"}
    assert "pw" not in repr(exposed)


def test_cause_is_preserved_but_not_exposed() -> None:
    """The underlying exception must be reachable for logs and absent from the client view."""
    cause = RuntimeError("password=hunter2 at db.internal:5432")
    error = InternalError("wrapped", cause=cause)
    assert error.__cause__ is cause
    assert error.client_message == "An unexpected error occurred."
    assert "hunter2" not in error.client_message


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_retry"),
    [
        (ValidationError("x"), 400, False),
        (ConfigurationError("x"), 500, False),
        (AuthorizationError("x"), 403, False),
        (NotFoundError("x"), 404, False),
        (ConflictError("x"), 409, False),
        (RateLimitedError("x", retry_after_seconds=5, scope="tenant"), 429, True),
        (DependencyUnavailableError("x", dependency="postgres"), 503, True),
        (TimeoutError_("x", dependency="postgres", timeout_seconds=1.0), 504, True),
        (InternalError("x"), 500, False),
    ],
)
def test_status_and_retryability_are_per_kind(
    error: ApplicationError, expected_status: int, expected_retry: bool
) -> None:
    """Status and retryability must be properties of the kind, not of the call site."""
    assert error.http_status == expected_status
    assert error.retryable is expected_retry


def test_codes_are_stable_strings() -> None:
    """Codes are the client-facing contract, so they must be plain stable strings."""
    for kind in ErrorKind:
        assert isinstance(kind.value, str)
        assert kind.value == kind.value.lower()
        assert " " not in kind.value


def test_rate_limit_carries_retry_after() -> None:
    """A 429 without a retry hint is not actionable for a client."""
    error = RateLimitedError("slow down", retry_after_seconds=30, scope="principal")
    assert error.public_context()["retry_after_seconds"] == 30
    assert error.public_context()["scope"] == "principal"


def test_unclassified_errors_map_to_internal_not_a_leak() -> None:
    """An exception that is not an ApplicationError must not be representable as a client error.

    The interface layer handles that case; this test pins the expectation that the base class
    defaults to the safest kind rather than, say, validation.
    """
    assert ApplicationError("x").kind is ErrorKind.INTERNAL

"""Unit tests for structured-logging redaction and schema guarantees.

Logs are the most widely-read and least-reviewed artefact a service produces: they are pasted
into tickets, shipped to aggregators and readable by anyone with dashboard access. The
redaction processor is therefore treated here as a security control, and attacked with
PII-shaped input rather than one obvious case.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from knowledge_assistant.observability.logging import (
    _REQUIRED_FIELDS,
    MAX_VALUE_CHARS,
    REDACTED_KEYS,
    REDACTED_PLACEHOLDER,
    SCHEMA_VERSION,
    add_required_fields,
    bind_request,
    redact_processor,
)

pytestmark = pytest.mark.unit


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    """Run an event through the redaction processor.

    Args:
        event: Event dictionary to redact.

    Returns:
        The redacted dictionary.

    """
    return dict(redact_processor(None, "info", dict(event)))


class TestSecretRedaction:
    """Credentials must never survive into a log line."""

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "db_password",
            "PASSWORD",
            "api_key",
            "API_KEY",
            "authorization",
            "token",
            "access_token",
            "refresh_token",
            "secret",
            "client_secret",
            "private_key",
            "cookie",
            "session_id",
        ],
    )
    def test_credential_keys_are_masked(self, key: str) -> None:
        """The denylist is substring-based on purpose: ``db_password`` and ``X-Api-Key`` are
        the names people actually use, not the ones a strict allow-list would predict.
        """
        assert _redact({key: "s3cr3t-value"})[key] == REDACTED_PLACEHOLDER

    def test_redaction_is_case_insensitive(self) -> None:
        assert _redact({"AuThOrIzAtIoN": "Bearer abc"})["AuThOrIzAtIoN"] == REDACTED_PLACEHOLDER

    def test_nested_mapping_is_redacted(self) -> None:
        """Credentials arrive nested, inside connection config and request context."""
        event = _redact({"request": {"headers": {"authorization": "Bearer abc"}, "id": "r1"}})
        assert event["request"]["headers"]["authorization"] == REDACTED_PLACEHOLDER
        assert event["request"]["id"] == "r1", "redaction must not destroy safe fields"

    def test_list_of_mappings_is_redacted(self) -> None:
        event = _redact({"users": [{"email": "a@b.c", "api_key": "k"}]})
        assert event["users"][0]["api_key"] == REDACTED_PLACEHOLDER

    def test_masked_value_cannot_be_recovered(self) -> None:
        """The masked value must be a fixed placeholder, not a truncation of the original -
        a truncated secret is still a partial secret.
        """
        out = _redact({"password": "abcdefghijklmnop"})["password"]
        assert out == REDACTED_PLACEHOLDER
        assert "abcdefgh" not in out

    def test_denylist_covers_the_obvious_credentials(self) -> None:
        """Guards against a refactor quietly shrinking the denylist."""
        for needle in ("password", "token", "secret", "api_key", "authorization"):
            assert any(needle in key for key in REDACTED_KEYS), needle


class TestValueTruncation:
    """Unbounded values turn a logging library into a storage denial-of-service."""

    def test_long_string_is_truncated(self) -> None:
        out = _redact({"note": "x" * 10_000})["note"]
        assert len(out) <= MAX_VALUE_CHARS
        assert out.endswith("...")

    def test_short_values_are_untouched(self) -> None:
        """Over-truncation destroys debuggability; the limit is a ceiling, not a policy."""
        assert _redact({"route": "/readyz"})["route"] == "/readyz"

    def test_truncation_applies_inside_containers(self) -> None:
        out = _redact({"context": {"blob": "y" * 10_000}})["context"]["blob"]
        assert len(out) <= MAX_VALUE_CHARS

    def test_truncation_is_not_applied_to_masked_keys_in_a_leaky_way(self) -> None:
        """A masked key must not be replaced by a truncated version of the secret."""
        out = _redact({"token": "z" * 10_000})["token"]
        assert out == REDACTED_PLACEHOLDER


class TestSchema:
    """Every line must carry the fields a log query depends on."""

    def test_required_fields_are_declared(self) -> None:
        for field in ("ts", "level", "event", "service", "env", "schema"):
            assert field in _REQUIRED_FIELDS

    def test_required_fields_are_added(self) -> None:
        event = add_required_fields(None, "info", {"event": "test"})
        assert event["schema"] == SCHEMA_VERSION
        assert "ts" in event

    def test_supplied_timestamp_is_not_overwritten(self) -> None:
        """A caller that has a precise timestamp keeps it; overriding would silently
        destroy ordering information.
        """
        event = add_required_fields(None, "info", {"ts": "2026-03-01T12:00:00Z"})
        assert event["ts"] == "2026-03-01T12:00:00Z"

    def test_redacted_event_is_json_serialisable(self) -> None:
        """The output must survive the renderer. A processor that returns a non-primitive
        fails in the logging path, which is the worst possible place to fail.
        """
        event = _redact({"a": 1, "b": [1, 2], "c": {"d": None}, "password": "p"})
        json.dumps(event)  # must not raise


class TestRequestBinding:
    """Request correlation is what makes a single request followable across logs."""

    def test_bound_fields_appear_in_the_event(self) -> None:
        import structlog  # noqa: PLC0415

        structlog.contextvars.clear_contextvars()
        bind_request(request_id="req-123", method="GET", path="/readyz")
        event = redact_processor(None, "info", dict(structlog.contextvars.get_contextvars()))
        assert event["request_id"] == "req-123"
        assert event["http_route"] == "/readyz"

    def test_bound_secrets_are_still_redacted(self) -> None:
        """Context binding must not be a way around the denylist."""
        import structlog  # noqa: PLC0415

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(authorization="Bearer leaked")
        bind_request(request_id="req-1", method="GET", path="/readyz")
        event = redact_processor(None, "info", dict(structlog.contextvars.get_contextvars()))
        assert event["authorization"] == REDACTED_PLACEHOLDER
        assert "leaked" not in json.dumps(event)
        structlog.contextvars.clear_contextvars()

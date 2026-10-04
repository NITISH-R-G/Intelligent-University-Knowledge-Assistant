"""Unit tests for the identifier contract.

Identifiers carry more weight than their size suggests. An idempotency key is a client's
intent token: it is echoed into a response header, written to a database column and passed
through the logging pipeline. These tests pin the documented behaviour of
:mod:`knowledge_assistant.domain.identifiers` so a future refactor cannot quietly change
what a valid key is, which exception is raised, or whether validation mutates its input.
"""

from __future__ import annotations

import uuid

import pytest

from knowledge_assistant.domain.errors import ApplicationError, ErrorKind, ValidationError
from knowledge_assistant.domain.identifiers import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    is_valid_idempotency_key,
    namespaced_uuid,
    new_id,
    new_request_id,
    validate_idempotency_key,
)

pytestmark = pytest.mark.unit

VALID_KEY = "order-4f2a9c1e-8b7d"


class TestValidKey:
    """Acceptance."""

    def test_well_formed_key_is_accepted(self) -> None:
        assert validate_idempotency_key(VALID_KEY) == VALID_KEY

    @pytest.mark.parametrize("boundary_length", [IDEMPOTENCY_KEY_MIN_LENGTH, IDEMPOTENCY_KEY_MAX_LENGTH])
    def test_length_bounds_are_inclusive(self, boundary_length: int) -> None:
        """Both bounds are documented as inclusive. An off-by-one here would reject a key a
        client is entitled to send, which surfaces as a confusing 400 rather than a clear one."""
        key = "a" * boundary_length
        assert is_valid_idempotency_key(key)
        assert validate_idempotency_key(key) == key

    @pytest.mark.parametrize("char", list("azAZ09._~-"))
    def test_every_documented_character_is_allowed(self, char: str) -> None:
        key = f"a{char * 15}"
        assert is_valid_idempotency_key(key), f"{char!r} is in the documented allow-list"

    def test_all_documented_characters_together(self) -> None:
        key = "aZ09._~-" * 3
        assert is_valid_idempotency_key(key)


class TestInvalidKey:
    """Rejection, and how rejection is signalled."""

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "a" * (IDEMPOTENCY_KEY_MIN_LENGTH - 1),
            "a" * (IDEMPOTENCY_KEY_MAX_LENGTH + 1),
            "a" * 20 + " " + "b" * 5,
            "a" * 20 + "/" + "b" * 5,
            "a" * 20 + "\n" + "b" * 5,
            "a" * 20 + "%00" + "b" * 5,
            "caf" + "\u00e9" * 20,
        ],
    )
    def test_malformed_keys_are_rejected(self, key: str) -> None:
        assert not is_valid_idempotency_key(key)
        with pytest.raises(ValidationError):
            validate_idempotency_key(key)

    def test_raises_the_documented_exception_type(self) -> None:
        """The docstring promises ``ValidationError``. It is not a ``ValueError``; callers
        must catch the taxonomy type, and this test keeps the two from being conflated."""
        with pytest.raises(ValidationError):
            validate_idempotency_key("bad")

    def test_error_is_an_application_error(self) -> None:
        """It must flow through the error taxonomy so the policy table applies to it."""
        with pytest.raises(ApplicationError) as excinfo:
            validate_idempotency_key("bad")
        assert excinfo.value.kind is ErrorKind.VALIDATION

    def test_error_message_echoes_none_of_the_supplied_value(self) -> None:
        """The key travels into logs. Echoing a malformed value back would let a client inject
        content - control characters, a fake log line - into the log stream."""
        hostile = "a" * 20 + "\nFAKE-LOG-LINE injected" + "b" * 5
        with pytest.raises(ValidationError) as excinfo:
            validate_idempotency_key(hostile)
        message = str(excinfo.value)
        assert "FAKE-LOG-LINE" not in message
        assert "\n" not in message

    def test_error_names_the_offending_field(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            validate_idempotency_key("bad")
        assert excinfo.value.public_context().get("field") == "idempotency_key"


class TestValidationDoesNotMutate:
    """Validation must not change meaning."""

    @pytest.mark.parametrize(
        "key",
        ["UPPER-Case-Key-1234", "key_with_underscores", "key.with.dots.here", "~tilde~key~here~"],
    )
    def test_key_is_returned_unchanged(self, key: str) -> None:
        """Case-folding or trimming would merge two distinct client intents, and a merged
        idempotency namespace returns the wrong cached response."""
        assert validate_idempotency_key(key) == key

    def test_leading_and_trailing_whitespace_is_a_rejection_not_a_trim(self) -> None:
        """Trimming would make a malformed key valid and silently change which key was
        meant. Rejecting is the only safe option."""
        assert not is_valid_idempotency_key(f"  {VALID_KEY}  ")


class TestRequestIdGeneration:
    """Request identifiers must be unique enough to correlate and short enough to log."""

    def test_request_ids_are_distinct(self) -> None:
        ids = {new_request_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_request_id_satisfies_the_idempotency_key_grammar(self) -> None:
        """Middleware uses this grammar to decide whether to trust a client-supplied id. The
        generated id must pass the same check, or tracing works only for client-supplied ids."""
        assert is_valid_idempotency_key(new_request_id())

    def test_generated_ids_are_unique(self) -> None:
        assert len({new_id() for _ in range(1000)}) == 1000

    def test_new_id_is_a_uuid(self) -> None:
        assert isinstance(new_id(), uuid.UUID)


class TestNamespacedUuid:
    """Content addressing depends on determinism."""

    def test_same_name_same_id(self) -> None:
        assert namespaced_uuid("doc-42") == namespaced_uuid("doc-42")

    def test_different_name_different_id(self) -> None:
        assert namespaced_uuid("doc-42") != namespaced_uuid("doc-43")

    def test_is_version_5(self) -> None:
        """UUIDv5 is specified as deterministic. A change of version would silently change
        every previously derived identifier in the database."""
        assert namespaced_uuid("doc-42").version == 5
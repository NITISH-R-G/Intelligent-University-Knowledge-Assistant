"""Application error taxonomy.

Every failure the system can produce is one of a small, closed set of kinds. Each kind has a
single, stable, machine-readable code, one HTTP status, one retryability answer, one log level
and one exposure rule.

Three properties matter more than the individual codes:

**Closed set.** Clients branch on ``code``, never on prose. A closed set means the mapping from
kind to behaviour is a total function, so it can be exhaustively tested. ``test_every_kind_has_a
_complete_policy`` asserts totality.

**Separation of internal from external.** An error carries a safe ``client_message`` and an
arbitrary ``context`` dict. Only ``context`` keys on the per-kind allow-list are ever serialised.
This is the mechanism that stops a database DSN or a stack fragment leaking through an error
response - see ``tests/unit/test_error_taxonomy.py::test_context_is_filtered_by_allowlist``.

**Retryability is a property of the kind, not of the call site.** Two callers failing the same way
must agree on whether a retry is sensible, otherwise retry storms appear in one path and not
another.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "ErrorKind",
    "ErrorPolicy",
    "ApplicationError",
    "ConfigurationError",
    "ValidationError",
    "AuthenticationError",
    "AuthorizationError",
    "NotFoundError",
    "ConflictError",
    "RateLimitedError",
    "DependencyUnavailableError",
    "TimeoutError_",
    "InternalError",
]


class ErrorKind(enum.StrEnum):
    """Closed set of failure kinds. Values are the stable wire codes."""

    VALIDATION = "validation_failed"
    CONFIGURATION = "configuration_invalid"
    AUTHENTICATION = "authentication_failed"
    AUTHORIZATION = "authorization_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    TIMEOUT = "timeout"
    INTERNAL = "internal_error"


class ErrorPolicy(enum.StrEnum):
    """How much of an error the outside world is allowed to learn."""

    #: Safe generic message and allow-listed context only.
    SAFE = "safe"
    #: Client may retry the same request unchanged.
    RETRY_SAME = "retry_same"
    #: Client may retry, but should expect a different outcome.
    RETRY_WITH_BACKOFF = "retry_with_backoff"


#: Context keys permitted to cross the trust boundary for each kind. Anything not
#: listed here is dropped at serialisation time.
_ALLOWED_CONTEXT_KEYS: Final[Mapping[ErrorKind, frozenset[str]]] = {
    ErrorKind.VALIDATION: frozenset({"field", "violations"}),
    ErrorKind.CONFIGURATION: frozenset({"setting"}),
    ErrorKind.AUTHENTICATION: frozenset({"scheme"}),
    ErrorKind.AUTHORIZATION: frozenset({"action", "resource_type"}),
    ErrorKind.NOT_FOUND: frozenset({"resource_type"}),
    ErrorKind.CONFLICT: frozenset({"resource_type", "reason"}),
    ErrorKind.RATE_LIMITED: frozenset({"scope", "retry_after_seconds"}),
    ErrorKind.DEPENDENCY_UNAVAILABLE: frozenset({"dependency"}),
    ErrorKind.TIMEOUT: frozenset({"dependency", "timeout_seconds"}),
    ErrorKind.INTERNAL: frozenset(),
}


#: The authoritative policy per kind. Frozen at import: policy is not runtime-configurable,
#: because a client must never be able to learn a different contract by asking.
POLICY_BY_KIND: Final[Mapping[ErrorKind, Mapping[str, Any]]] = {
    ErrorKind.VALIDATION: {
        "http_status": 400,
        "retryable": False,
        "log_level": "info",
        "policy": ErrorPolicy.SAFE,
        "client_message": "The request was invalid.",
    },
    ErrorKind.CONFIGURATION: {
        "http_status": 500,
        "retryable": False,
        "log_level": "critical",
        "policy": ErrorPolicy.SAFE,
        "client_message": "The service is misconfigured and cannot serve requests.",
    },
    ErrorKind.AUTHENTICATION: {
        "http_status": 401,
        "retryable": False,
        "log_level": "info",
        "policy": ErrorPolicy.SAFE,
        "client_message": "Authentication is required.",
    },
    ErrorKind.AUTHORIZATION: {
        "http_status": 403,
        "retryable": False,
        "log_level": "warning",
        "policy": ErrorPolicy.SAFE,
        "client_message": "You are not permitted to perform this action.",
    },
    ErrorKind.NOT_FOUND: {
        "http_status": 404,
        "retryable": False,
        "log_level": "info",
        "policy": ErrorPolicy.SAFE,
        "client_message": "The requested resource does not exist.",
    },
    ErrorKind.CONFLICT: {
        "http_status": 409,
        "retryable": False,
        "log_level": "info",
        "policy": ErrorPolicy.SAFE,
        "client_message": "The request conflicts with the current state of the resource.",
    },
    ErrorKind.RATE_LIMITED: {
        "http_status": 429,
        "retryable": True,
        "log_level": "warning",
        "policy": ErrorPolicy.RETRY_WITH_BACKOFF,
        "client_message": "Too many requests. Retry after the indicated delay.",
    },
    ErrorKind.DEPENDENCY_UNAVAILABLE: {
        "http_status": 503,
        "retryable": True,
        "log_level": "error",
        "policy": ErrorPolicy.RETRY_WITH_BACKOFF,
        "client_message": "A required dependency is unavailable. Retry shortly.",
    },
    ErrorKind.TIMEOUT: {
        "http_status": 504,
        "retryable": True,
        "log_level": "error",
        "policy": ErrorPolicy.RETRY_WITH_BACKOFF,
        "client_message": "The request timed out. Retry shortly.",
    },
    ErrorKind.INTERNAL: {
        "http_status": 500,
        "retryable": False,
        "log_level": "error",
        "policy": ErrorPolicy.SAFE,
        "client_message": "An unexpected error occurred.",
    },
}


class ApplicationError(Exception):
    """Base class for every error the application raises deliberately.

    Attributes:
        kind: Which taxonomy entry applies.
        detail: Operator-only context. Never serialised to a client except for
            allow-listed keys.

    """

    kind: ErrorKind = ErrorKind.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Build the error.

        Args:
            message: Operator-facing description. May contain sensitive material and is
                therefore only ever logged, never returned.
            detail: Structured operator context. Filtered by allow-list on serialisation.
            cause: Underlying exception, preserved for logging only.

        """
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})
        self.__cause__ = cause

    @property
    def policy(self) -> Mapping[str, Any]:
        """Return the frozen policy for this error's kind."""
        return POLICY_BY_KIND[self.kind]

    @property
    def code(self) -> str:
        """Return the stable machine-readable code."""
        return self.kind.value

    @property
    def http_status(self) -> int:
        """Return the HTTP status code."""
        return int(self.policy["http_status"])

    @property
    def retryable(self) -> bool:
        """Return whether retrying the identical request could succeed."""
        return bool(self.policy["retryable"])

    @property
    def log_level(self) -> str:
        """Return the log level this error should be logged at."""
        return str(self.policy["log_level"])

    @property
    def client_message(self) -> str:
        """Return the safe, generic message shown to clients."""
        return str(self.policy["client_message"])

    def public_context(self) -> dict[str, Any]:
        """Return only the context keys permitted for this kind.

        This is the single chokepoint that prevents internal detail from reaching a client.
        """
        allowed = _ALLOWED_CONTEXT_KEYS[self.kind]
        return {k: v for k, v in self.detail.items() if k in allowed}


class ValidationError(ApplicationError):
    """The request was malformed or violated a documented rule."""

    kind = ErrorKind.VALIDATION


class ConfigurationError(ApplicationError):
    """Configuration is missing, malformed, or fails a safety rule.

    Raised during startup, never during request handling. ``ConfigurationError`` therefore
    signals a deployment fault, not a user fault, and is logged at ``critical``.
    """

    kind = ErrorKind.CONFIGURATION


class AuthenticationError(ApplicationError):
    """Caller identity could not be established."""

    kind = ErrorKind.AUTHENTICATION


class AuthorizationError(ApplicationError):
    """Caller identity is established but not permitted."""

    kind = ErrorKind.AUTHORIZATION


class NotFoundError(ApplicationError):
    """The resource does not exist, or must be reported as if it does not.

    Used for both genuinely missing resources and for resources the caller may not see.
    Collapsing the two prevents existence disclosure, which is a real leak in a
    permissioned system.
    """

    kind = ErrorKind.NOT_FOUND


class ConflictError(ApplicationError):
    """The request conflicts with current state (e.g. optimistic lock failure)."""

    kind = ErrorKind.CONFLICT


class RateLimitedError(ApplicationError):
    """Caller exceeded a quota or rate limit."""

    kind = ErrorKind.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int,
        scope: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Build a rate-limit error carrying an explicit retry hint."""
        super().__init__(
            message,
            detail={
                **dict(detail or {}),
                "retry_after_seconds": retry_after_seconds,
                "scope": scope,
            },
        )
        self.retry_after_seconds = retry_after_seconds
        self.scope = scope


class DependencyUnavailableError(ApplicationError):
    """A required collaborator is unavailable.

    Distinct from :class:`TimeoutError_` so that alerting can separate "the database is
    refusing connections" from "the database is too slow".
    """

    kind = ErrorKind.DEPENDENCY_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        dependency: str,
        detail: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Build a dependency failure error naming the dependency."""
        super().__init__(
            message,
            detail={**dict(detail or {}), "dependency": dependency},
            cause=cause,
        )
        self.dependency = dependency


class TimeoutError_(ApplicationError):  # noqa: N801, N818 - see docstring
    """A collaborator exceeded its time budget.

    Named with a trailing underscore to avoid shadowing the builtin. The bare builtin
    ``TimeoutError`` remains available for genuine language-level timeouts.

    Two naming rules are deliberately broken here, and only here:

    - **N801** (CapWords) rejects the trailing underscore. There is no alternative spelling
      that avoids shadowing the builtin ``TimeoutError``, and shadowing it would silently
      change the meaning of unrelated ``except TimeoutError`` clauses in this module.
    - **N818** (Exception ``Error`` suffix) is satisfied in spirit: the class is an error and
      the trailing underscore is the only part that deviates from the convention.
    """

    kind = ErrorKind.TIMEOUT

    def __init__(
        self,
        message: str,
        *,
        dependency: str,
        timeout_seconds: float,
        detail: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Build a timeout error carrying the budget that was exceeded."""
        super().__init__(
            message,
            detail={
                **dict(detail or {}),
                "dependency": dependency,
                "timeout_seconds": timeout_seconds,
            },
            cause=cause,
        )
        self.dependency = dependency
        self.timeout_seconds = timeout_seconds


class InternalError(ApplicationError):
    """An unexpected condition. Always a bug until proven otherwise."""

    kind = ErrorKind.INTERNAL

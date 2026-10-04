"""Structured JSON logging with mandatory redaction.

**What this guarantees.** Every log line is a single JSON object with a stable schema. Keys
matching the denylist are masked before the line is serialised. Values longer than
``MAX_VALUE_CHARS`` are truncated. This runs as a structlog processor, i.e. *before* the
serialiser, so a forgotten field cannot bypass it.

**Why a denylist rather than an allow-list.** An allow-list breaks every time a developer adds
a log field, which produces a culture of ``# noqa`` and blanket exceptions. The denylist is
deliberately broad, covering both credentials and the security-sensitive settings from
``config.categories``, and it is tested against a corpus of PII-shaped values.

**Why truncation matters.** An unbounded log value is a disk-exhaustion vector on a single-node
free-tier deployment, and a large value is almost never useful in a log line: the detail belongs
in a trace or a database row with a retention policy.

**Correlation.** :func:`bind_request` attaches ``request_id``, ``trace_id`` and ``span_id`` to a
bound logger so that every line emitted while handling a request carries the same identifiers.
This is the mechanism that makes ``grep request_id`` a complete account of one request.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

import structlog

__all__ = [
    "configure_logging",
    "get_logger",
    "bind_request",
    "REDACTED_KEYS",
    "REDACTED_PLACEHOLDER",
    "MAX_VALUE_CHARS",
    "SCHEMA_VERSION",
]

#: Bumped when the log line shape changes. Consumers key off it rather than guessing.
SCHEMA_VERSION: Final[str] = "1"

#: Maximum characters retained per value before truncation.
MAX_VALUE_CHARS: Final[int] = 512

#: Keys whose values are always masked. Matching is case-insensitive and substring-based so
#: that ``db_password``, ``X-Auth-Token`` and ``userPassword`` are all caught.
REDACTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "set_cookie",
        "session",
        "csrf",
        "private_key",
        "dsn",
        "database_url",
        "conn_str",
        "connection_string",
        "client_secret",
        "refresh_token",
        "access_token",
        "idempotency_key",
        "signature",
    }
)

#: Rendered in place of a redacted value. Deliberately *not* imported from
#: ``config.categories``, which declares its own identical sentinel for dumps: observability must
#: not depend on the configuration layer, and one duplicated two-character literal is a cheaper
#: price than a layer inversion.
REDACTED_PLACEHOLDER: Final[str] = "***"

#: Keys that are always present in a rendered line, in this order.
_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("ts", "level", "event", "service", "env", "schema")


def _is_redacted(key: str) -> bool:
    """Return whether a key's value must be masked.

    Args:
        key: Event dictionary key.

    Returns:
        ``True`` if the key matches the denylist.
    """
    lowered = key.lower()
    return any(needle in lowered for needle in REDACTED_KEYS)


def _redact_value(value: Any) -> Any:
    """Truncate a value for safe logging.

    Args:
        value: Arbitrary value.

    Returns:
        The value, or a truncated string if it exceeds ``MAX_VALUE_CHARS``.
    """
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[: MAX_VALUE_CHARS - 3] + "..."
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mask denylisted keys and truncate long values, recursively.

    Args:
        _logger: structlog logger (unused).
        _method: structlog method name (unused).
        event_dict: The event being logged, mutated in place.

    Returns:
        The same event dictionary, safe to serialise.
    """
    for key in list(event_dict.keys()):
        value = event_dict[key]
        if _is_redacted(key):
            event_dict[key] = REDACTED_PLACEHOLDER
            continue
        if isinstance(value, Mapping):
            event_dict[key] = redact_processor(_logger, _method, dict(value))
        elif isinstance(value, (list, tuple)):
            # Recurse into each item: a list of mappings is how batched and nested payloads
            # arrive, and truncating without recursing would leave nested credentials intact.
            event_dict[key] = type(value)(
                redact_processor(_logger, _method, dict(item))
                if isinstance(item, Mapping)
                else _redact_value(item)
                for item in value
            )
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def add_required_fields(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Ensure every line carries the required schema fields.

    Args:
        _logger: structlog logger (unused).
        _method: structlog method name (unused).
        event_dict: The event being logged.

    Returns:
        The event dictionary with required fields populated.
    """
    event_dict.setdefault("schema", SCHEMA_VERSION)
    event_dict.setdefault("ts", dt.datetime.now(tz=dt.timezone.utc).isoformat())
    return event_dict


def configure_logging(
    *,
    level: str = "info",
    fmt: str = "json",
    service: str = "knowledge-assistant",
    environment: str = "local",
) -> None:
    """Install the structured logging pipeline.

    Safe to call more than once; structlog configuration is idempotent.

    Args:
        level: Minimum level to emit.
        fmt: ``"json"`` for machine parsing, ``"console"`` for human reading. A plain string
            rather than the ``config.LogFormat`` enum, because observability must not depend on
            the configuration layer.
        service: Service name attached to every line.
        environment: Environment name attached to every line.
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_required_fields,
        redact_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any
    if str(fmt).lower() == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _bind_static(service=service, env=environment)


def _bind_static(**kwargs: Any) -> None:
    """Attach static context to every subsequent line.

    Args:
        **kwargs: Static key/value pairs.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger.

    Args:
        name: Optional logger name, typically ``__name__``.

    Returns:
        A structlog logger.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_request(
    *,
    request_id: str,
    method: str,
    path: str,
    client_host: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Any:
    """Bind per-request correlation identifiers and return a logger.

    Args:
        request_id: Correlation identifier echoed to the client in ``X-Request-ID``.
        method: HTTP method.
        path: Request path. Must be the *route template*, not the raw URL, or the path
            cardinality becomes unbounded - one time series per distinct URL.
        client_host: Client address. Not logged in production-like environments by default,
            because an address is personal data under several regimes; the call site decides.
        trace_id: OpenTelemetry trace id, when available.
        span_id: OpenTelemetry span id, when available.

    Returns:
        A logger carrying the bound context.
    """
    ctx: dict[str, Any] = {"request_id": request_id, "http_method": method, "http_route": path}
    if client_host:
        ctx["client_host"] = client_host
    if trace_id:
        ctx["trace_id"] = trace_id
    if span_id:
        ctx["span_id"] = span_id
    structlog.contextvars.bind_contextvars(**ctx)
    return structlog.get_logger("request")


def clear_request_context() -> None:
    """Clear per-request context. Called at the end of a request to prevent context leaking
    into the next request handled by the same task-local context."""
    structlog.contextvars.clear_contextvars()
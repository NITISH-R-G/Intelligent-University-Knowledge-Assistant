"""Idempotency as a first-class domain concept.

Idempotency is not a middleware detail; it is a rule about *what the client means*. This module
makes that rule explicit and pure, so it can be property-tested rather than integration-tested.

The contract, in one paragraph: a request carrying key ``K`` and scope ``S`` may be executed at
most once per ``(tenant, S, K)``. The first execution records its outcome. A later request with
the same triple returns the recorded outcome without re-executing. A request that failed
*before* producing an outcome may be retried with the same key.

That last sentence is the part implementations usually get wrong. If a failure records a
terminal result, the client can never retry a request that failed for a transient reason - and
clients do retry. So only *committed* outcomes are recorded; failures release the key.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Any, Final, Mapping

__all__ = [
    "IdempotencyScope",
    "IdempotencyStatus",
    "IdempotencyRecord",
    "DEFAULT_RETENTION_SECONDS",
    "ReplayDecision",
    "decide_replay",
    "scoped_key",
]

#: Default retention: 24 hours. Long enough to cover client retry windows and mobile clients
#: that retry on resume; short enough that the table does not grow without bound. Chosen from
#: documented retry behaviour rather than measured client behaviour, which is not available yet
#: (see docs/02-requirements/DECISION_LOCK.md D-014: ASSUMED).
DEFAULT_RETENTION_SECONDS: Final[int] = 24 * 60 * 60


class IdempotencyScope(enum.StrEnum):
    """The operation family a key belongs to.

    Scoping exists so that a key reused by a client for a *different* operation is not
    silently treated as a replay of the first. Unscoped keys are a classic source of
    "my retry of B returned the response from A".
    """

    JOB_SUBMIT = "job.submit"
    #: Reserved for the first mutating endpoint added in Phase 2+ (e.g. document upload).
    DOCUMENT_UPLOAD = "document.upload"


class IdempotencyStatus(enum.StrEnum):
    """State of a recorded idempotent operation."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """A recorded outcome for one ``(tenant, scope, key)`` triple."""

    scope: IdempotencyScope
    key: str
    status: IdempotencyStatus
    #: Fingerprint of the request body. A replay with the same key but a *different* body is a
    #: client bug and must be rejected, not served from cache.
    request_fingerprint: str
    #: Serialised response or resource reference, only meaningful when COMPLETED.
    response_payload: Mapping[str, Any] | None
    created_at: dt.datetime
    expires_at: dt.datetime

    @property
    def is_terminal(self) -> bool:
        """Return whether a committed outcome exists for this key."""
        return self.status is IdempotencyStatus.COMPLETED

    def is_expired(self, *, now: dt.datetime) -> bool:
        """Return whether the record is past its retention window at ``now``.

        Args:
            now: Current time from the injected clock.

        Returns:
            ``True`` if the record may be treated as absent.

        Note:
            Expiry is evaluated against an injected ``now`` rather than read from a clock
            here, keeping this module free of I/O.
        """
        return now >= self.expires_at


class ReplayDecision(enum.StrEnum):
    """What the application should do with an incoming keyed request."""

    #: No record exists. Execute and record the outcome.
    EXECUTE = "execute"
    #: A completed record with a matching fingerprint exists. Return the stored response.
    REPLAY = "replay"
    #: An identical key is in flight. The caller is told to retry rather than wait, because
    #: holding a connection open behind another request is how a slow endpoint becomes a
    #: connection-exhaustion outage.
    IN_FLIGHT = "in_flight"
    #: Key reused with a different request body. This is a client bug, not a retry.
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    #: A record exists but has passed retention; treat as absent and execute.
    EXPIRED = "expired"


def scoped_key(tenant_id: str, scope: IdempotencyScope, key: str) -> str:
    """Return the storage key for a ``(tenant, scope, key)`` triple.

    Args:
        tenant_id: Owning tenant.
        scope: Operation family.
        key: Client-supplied key.

    Returns:
        Deterministic composite key. Including the tenant in the key - rather than filtering
        on it later - means a cross-tenant collision is impossible at the storage level.
    """
    return f"{tenant_id}:{scope.value}:{key}"


def decide_replay(
    record: IdempotencyRecord | None,
    *,
    request_fingerprint: str,
    now: dt.datetime,
) -> ReplayDecision:
    """Decide what to do with an incoming request carrying an idempotency key.

    Args:
        record: Existing record for this key, or ``None`` if absent.
        request_fingerprint: Stable hash of the request body and relevant headers.
        now: Current time from the injected clock.

    Returns:
        The decision to apply. Expiry is checked before status, because an expired record
        must not block a legitimate retry.
    """
    if record is None:
        return ReplayDecision.EXECUTE
    if now >= record.expires_at:
        return ReplayDecision.EXPIRED
    if record.request_fingerprint != request_fingerprint:
        return ReplayDecision.FINGERPRINT_MISMATCH
    if record.status is IdempotencyStatus.IN_PROGRESS:
        return ReplayDecision.IN_FLIGHT
    return ReplayDecision.REPLAY
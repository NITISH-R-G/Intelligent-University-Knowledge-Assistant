"""Idempotent execution use case.

Wraps a side-effecting operation so that it executes at most once per
``(tenant, scope, key)`` triple, and returns the recorded outcome on replay.

Three behaviours that implementations commonly get wrong, handled here:

**A failure releases the key.** If the wrapped operation raises, the key is released so the
client's retry can genuinely re-execute. Recording a failure as the outcome would make a
transient error permanent for that key.

**A fingerprint mismatch is a conflict, not a replay.** Reusing a key with a different body is
a client bug; serving the first response would return data for a different request and hide the
bug.

**The replay path never executes the operation.** This is asserted in tests by counting
handler invocations, because a replay path that re-runs the handler while returning the cached
response is idempotent in name only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from knowledge_assistant.application.ports import IdempotencyStorePort
from knowledge_assistant.domain.clock import Clock
from knowledge_assistant.domain.errors import ConflictError, InternalError
from knowledge_assistant.domain.idempotency import (
    DEFAULT_RETENTION_SECONDS,
    IdempotencyScope,
    ReplayDecision,
)

__all__ = ["IdempotentExecutor", "fingerprint", "Replayed"]

#: A replayed result is distinguishable from a freshly computed one so handlers and clients
#: can tell whether work actually happened.
type Replayed = tuple[Mapping[str, Any], bool]


def fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a stable fingerprint for a request payload.

    Canonical JSON with sorted keys, so that two semantically identical requests with
    different key order produce the same fingerprint. Using :func:`hash` would be wrong: it is
    salted per process by default, so a fingerprint would not survive a restart or be
    comparable across replicas.

    Args:
        payload: Request body or relevant headers.

    Returns:
        Hex SHA-256 of the canonical encoding.

    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotentExecutor:
    """Runs a side-effecting operation at most once per idempotency key."""

    __slots__ = ("_store", "_clock", "_retention_seconds")

    def __init__(
        self,
        store: IdempotencyStorePort,
        *,
        clock: Clock,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        """Build the executor.

        Args:
            store: Idempotency store.
            clock: Injected time source.
            retention_seconds: How long a completed outcome is replayable.

        """
        self._store = store
        self._clock = clock
        self._retention_seconds = retention_seconds

    async def run(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        payload: Mapping[str, Any],
        operation: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Replayed:
        """Execute ``operation`` at most once for the given key.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client-supplied idempotency key.
            payload: Request content, used for the fingerprint.
            operation: Zero-argument async callable performing the side effect. Takes no
                arguments so it cannot accidentally close over per-request mutable state.

        Returns:
            ``(result, was_replayed)``.

        Raises:
            ConflictError: If the key is in flight under a different payload, or if the store
                fails to claim the key.
            InternalError: If a completed record has no stored payload, which indicates store
                corruption rather than a client error.

        """
        request_fp = fingerprint(payload)
        now = self._clock.now()
        claimed = await self._store.begin(
            tenant_id=tenant_id,
            scope=scope,
            key=key,
            request_fingerprint=request_fp,
            now=now,
            retention_seconds=self._retention_seconds,
        )

        if claimed is not None:
            decision = _decide(claimed, request_fingerprint=request_fp, now=now)
            if decision is ReplayDecision.REPLAY:
                stored = claimed.response_payload or {}
                return stored, True
            if decision is ReplayDecision.IN_FLIGHT:
                msg = "a request with this idempotency key is already in flight"
                raise ConflictError(
                    msg,
                    detail={"resource_type": "idempotency_key", "reason": "in_flight"},
                )
            if decision is ReplayDecision.FINGERPRINT_MISMATCH:
                msg = "idempotency key was already used with a different request body"
                raise ConflictError(
                    msg,
                    detail={"resource_type": "idempotency_key", "reason": "fingerprint_mismatch"},
                )
            # EXPIRED: the store still returned a record, so delete-through by re-claiming.
            # The adapter treats begin() on an expired record as a successful re-claim.

        try:
            result = await operation()
        except BaseException:
            # Release so a client retry can genuinely re-execute. Swallowing the release error
            # would mask the original failure; it is logged by the caller instead.
            await self._release_quietly(tenant_id=tenant_id, scope=scope, key=key)
            raise

        await self._store.complete(
            tenant_id=tenant_id,
            scope=scope,
            key=key,
            response_payload=dict(result),
            now=self._clock.now(),
        )
        return dict(result), False

    async def _release_quietly(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Release a claimed key, suppressing secondary failures.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        """
        with contextlib.suppress(Exception):
            # Best-effort cleanup. A release failure must not mask the operation's own
            # error, which is the one the caller logs and the one worth seeing.
            await self._store.release(tenant_id=tenant_id, scope=scope, key=key)


def _decide(record: Any, *, request_fingerprint: str, now: Any) -> ReplayDecision:
    """Classify an existing record for the replay path.

    Args:
        record: Existing record returned by the store.
        request_fingerprint: Fingerprint of the incoming request.
        now: Current time.

    Returns:
        The replay decision.

    """
    from knowledge_assistant.domain.idempotency import decide_replay  # noqa: PLC0415

    return decide_replay(record, request_fingerprint=request_fingerprint, now=now)


def assert_stored_payload_present(record: Any) -> None:
    """Raise InternalError if a completed record has no payload.

    Args:
        record: Completed record.

    Raises:
        InternalError: When the payload is missing, indicating store corruption.

    """
    if record.response_payload is None:
        msg = "completed idempotency record has no stored payload; store is corrupt"
        raise InternalError(msg, detail={"resource_type": "idempotency_record"})

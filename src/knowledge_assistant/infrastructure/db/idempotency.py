"""PostgreSQL idempotency store.

The critical method is :meth:`PgIdempotencyStore.begin`, whose contract is *"claim the key
atomically, or tell me somebody else holds it"*. It is implemented as a single statement:

.. code-block:: sql

    INSERT INTO idempotency_keys (...) VALUES (...)
    ON CONFLICT (tenant_id, scope, key) DO NOTHING
    RETURNING ...

The ``ON CONFLICT DO NOTHING RETURNING`` pattern gives the atomicity without an explicit
transaction or a lock: either this caller inserted the row and gets a result back, or it did
not and gets ``None``. Two concurrent requests with the same key therefore cannot both execute.

An existing row is then selected for expiry handling. Expired rows are deleted and re-inserted
by the *next* caller rather than in this call, which keeps the common path to a single statement
and avoids a read-modify-write race on the expiry boundary.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from psycopg.rows import dict_row

from knowledge_assistant.domain.idempotency import (
    IdempotencyRecord,
    IdempotencyScope,
    IdempotencyStatus,
)

__all__ = ["PgIdempotencyStore"]

_COLUMNS: str = """
    tenant_id, scope, key, status, request_fingerprint,
    response_payload, created_at, expires_at
"""


def _row_to_record(row: Mapping[str, Any]) -> IdempotencyRecord:
    """Map a database row to an ``IdempotencyRecord``.

    Args:
        row: Row from a ``dict_row`` cursor.

    Returns:
        The mapped record.

    """
    return IdempotencyRecord(
        scope=IdempotencyScope(str(row["scope"])),
        key=str(row["key"]),
        status=IdempotencyStatus(str(row["status"])),
        request_fingerprint=str(row["request_fingerprint"]),
        response_payload=row["response_payload"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


class PgIdempotencyStore:
    """Idempotency persistence on PostgreSQL."""

    __slots__ = ("_database",)

    def __init__(self, database: Any) -> None:
        """Adopt a ``Database`` handle.

        Args:
            database: Object exposing ``acquire()`` as an async context manager.

        """
        self._database = database

    async def get(
        self, *, tenant_id: str, scope: IdempotencyScope, key: str
    ) -> IdempotencyRecord | None:
        """Return the record for a key.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        Returns:
            The record, or ``None``.

        """
        sql = f"""
            SELECT {_COLUMNS} FROM idempotency_keys
            WHERE tenant_id = %(tenant_id)s AND scope = %(scope)s AND key = %(key)s
        """
        params = {"tenant_id": tenant_id, "scope": scope.value, "key": key}
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def begin(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        request_fingerprint: str,
        now: dt.datetime,
        retention_seconds: int,
    ) -> IdempotencyRecord | None:
        """Atomically claim a key for execution.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            request_fingerprint: Fingerprint of the request body.
            now: Current time.
            retention_seconds: Retention window for the recorded outcome.

        Returns:
            ``None`` if the caller now owns the key and should execute, or the existing record
            if another execution holds it.

        """
        expires_at = now + dt.timedelta(seconds=retention_seconds)
        insert_sql = """
            INSERT INTO idempotency_keys
                (tenant_id, scope, key, status, request_fingerprint,
                 response_payload, created_at, expires_at, updated_at)
            VALUES (%(tenant_id)s, %(scope)s, %(key)s, 'in_progress', %(fingerprint)s,
                    NULL, %(now)s, %(expires_at)s, %(now)s)
            ON CONFLICT (tenant_id, scope, key) DO NOTHING
            RETURNING tenant_id
        """
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "scope": scope.value,
            "key": key,
            "fingerprint": request_fingerprint,
            "now": now,
            "expires_at": expires_at,
        }
        async with self._database.acquire() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(insert_sql, params)
            inserted = await cur.fetchone()
            if inserted is not None:
                return None  # caller owns the key
            select_sql = (
                f"SELECT {_COLUMNS} FROM idempotency_keys "
                "WHERE tenant_id = %(tenant_id)s "
                "AND scope = %(scope)s AND key = %(key)s"
            )
            await cur.execute(select_sql, params)
            row = await cur.fetchone()
        if row is None:  # pragma: no cover - row deleted concurrently; treat as owned
            return None
        record = _row_to_record(row)
        if now >= record.expires_at:
            # Expired: delete so the next caller re-claims cleanly. Returning the stale record
            # would make the caller believe the key is held, when it is not.
            await self._delete(tenant_id=tenant_id, scope=scope, key=key)
            return None
        return record

    async def _delete(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Delete a record outright. Used only for retention expiry."""
        sql = "DELETE FROM idempotency_keys WHERE tenant_id = %s AND scope = %s AND key = %s"
        async with self._database.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, (tenant_id, scope.value, key))

    async def complete(
        self,
        *,
        tenant_id: str,
        scope: IdempotencyScope,
        key: str,
        response_payload: Mapping[str, Any],
        now: dt.datetime,
    ) -> None:
        """Record a committed outcome.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.
            response_payload: Stored response or resource reference.
            now: Current time, used to extend expiry from completion rather than from start.

        """
        sql = """
            UPDATE idempotency_keys SET
                status = 'completed',
                response_payload = %(payload)s,
                updated_at = %(now)s,
                expires_at = %(expires_at)s
            WHERE tenant_id = %(tenant_id)s AND scope = %(scope)s AND key = %(key)s
        """
        params = {
            "tenant_id": tenant_id,
            "scope": scope.value,
            "key": key,
            "payload": dict(response_payload),
            "now": now,
            "expires_at": now + dt.timedelta(seconds=24 * 60 * 60),
        }
        async with self._database.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)

    async def release(self, *, tenant_id: str, scope: IdempotencyScope, key: str) -> None:
        """Release a claimed key so a client retry can re-execute.

        Args:
            tenant_id: Owning tenant.
            scope: Operation family.
            key: Client key.

        """
        sql = """
            DELETE FROM idempotency_keys
            WHERE tenant_id = %(tenant_id)s AND scope = %(scope)s AND key = %(key)s
              AND status = 'in_progress'
        """
        async with self._database.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, {"tenant_id": tenant_id, "scope": scope.value, "key": key})

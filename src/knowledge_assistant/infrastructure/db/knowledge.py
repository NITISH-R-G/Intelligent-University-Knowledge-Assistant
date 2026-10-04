"""PostgreSQL + pgvector knowledge store.

Implements both :class:`VectorStorePort` and :class:`LexicalSearchPort` against the one
database the project already uses. No second store, no new service, no new dependency.

**Why the two ports are answered by one class.** They share a connection and a table, and
splitting them would mean two classes and two pools to keep in sync. The *ports* stay separate
because retrieval must be able to take either side alone - that is what makes the fusion
testable and what would allow one side to be replaced independently.

**Replace, never append.** ``upsert_chunks`` deletes the document's existing rows before
inserting. Appending would let an edited document leave orphaned chunks behind that retrieval
would happily cite - the failure mode of a knowledge assistant is quoting a deleted rule.

**Lexical search uses PostgreSQL full text**, via a generated ``tsvector`` column and GIN, not
a Python-side word count. That keeps the lexical ranking inside the database where it belongs,
survives restarts, and costs no extra dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from knowledge_assistant.domain.knowledge import Chunk, KnowledgeStats, RetrievedChunk
from knowledge_assistant.infrastructure.db.engine import translate_db_error

__all__ = ["PgKnowledgeStore"]


def _row_to_chunk(row: dict[str, Any]) -> Chunk:
    """Build a :class:`Chunk` from a database row.

    Args:
        row: Row from ``chunks`` joined with ``documents``.

    Returns:
        The chunk.

    """
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        document_title=row["document_title"],
        source=row["source"],
        section=row["section"] or "",
        position=int(row["position"]),
        text=row["text"],
    )


class PgKnowledgeStore:
    """pgvector-backed implementation of the knowledge store ports."""

    __slots__ = ("_database", "_provider_name")

    def __init__(self, database: Any) -> None:
        """Adopt a database handle.

        Args:
            database: Object exposing ``acquire()``.

        """
        self._database = database
        self._provider_name = "unknown"

    async def upsert_document(
        self, *, document_id: str, title: str, source: str, text: str, content_hash: str
    ) -> None:
        """Insert or update one document, ignoring an unchanged one.

        Args:
            document_id: Stable document id.
            title: Human-readable title.
            source: Citation source.
            text: Full document text.
            content_hash: Hash of ``text``.

        Raises:
            DependencyUnavailableError: If the database is unreachable.

        """
        sql = """
            INSERT INTO documents (id, title, source, content_hash, text, updated_at)
            VALUES (%(id)s, %(title)s, %(source)s, %(content_hash)s, %(text)s, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                source = EXCLUDED.source,
                content_hash = EXCLUDED.content_hash,
                text = EXCLUDED.text,
                updated_at = now()
            WHERE documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
        """
        params = {
            "id": document_id,
            "title": title,
            "source": source,
            "content_hash": content_hash,
            "text": text,
        }
        try:
            async with self._database.acquire() as conn:
                await conn.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - normalised into the error taxonomy
            raise translate_db_error(exc) from exc

    async def upsert_chunks(
        self, *, document_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        """Replace every chunk of a document with the supplied chunks and vectors.

        Args:
            document_id: Document whose chunks are being replaced.
            chunks: Chunks to store.
            vectors: One vector per chunk, in the same order.

        Returns:
            The number of chunks written.

        Raises:
            ValueError: If ``vectors`` is not the same length as ``chunks``.
            DependencyUnavailableError: If the database is unreachable.

        """
        if len(vectors) != len(chunks):
            msg = "vectors and chunks must be the same length"
            raise ValueError(msg)
        if not chunks:
            return 0

        delete_sql = "DELETE FROM chunks WHERE document_id = %(document_id)s"
        insert_sql = """
            INSERT INTO chunks (
                id, document_id, title, source, section, position, text, embedding, provider
            ) VALUES (
                %(id)s, %(document_id)s, %(title)s, %(source)s, %(section)s,
                %(position)s, %(text)s, %(embedding)s::vector, %(provider)s
            )
            ON CONFLICT (id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                title = EXCLUDED.title,
                source = EXCLUDED.source,
                section = EXCLUDED.section,
                position = EXCLUDED.position,
                text = EXCLUDED.text,
                embedding = EXCLUDED.embedding,
                provider = EXCLUDED.provider
        """
        rows: list[dict[str, Any]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            rows.append(
                {
                    "id": chunk.id,
                    "document_id": chunk.document_id,
                    "title": chunk.document_title,
                    "source": chunk.source,
                    "section": chunk.section,
                    "position": chunk.position,
                    "text": chunk.text,
                    "embedding": "[" + ",".join(f"{value:.8f}" for value in vector) + "]",
                    "provider": self._provider_name,
                }
            )

        try:
            async with self._database.acquire() as conn, conn.transaction():
                await conn.execute(delete_sql, {"document_id": document_id})
                for row in rows:
                    await conn.execute(insert_sql, row)
        except Exception as exc:  # noqa: BLE001 - normalised into the error taxonomy
            raise translate_db_error(exc) from exc
        return len(rows)

    def with_provider(self, provider_name: str) -> PgKnowledgeStore:
        """Return a copy of this store that records ``provider_name`` on written vectors.

        The provider name travels with every stored vector so that a corpus embedded by one
        model is never silently queried by another. Mismatched dimensions raise at query time;
        a mismatched *name* would not, and would quietly degrade every score.

        Args:
            provider_name: Identifier of the embedding provider in use.

        Returns:
            A store bound to that provider name.

        """
        bound = PgKnowledgeStore(self._database)
        bound._provider_name = provider_name  # noqa: SLF001 - deliberate, same class
        return bound

    async def search_vector(
        self, *, query_vector: Sequence[float], limit: int, provider: str | None = None
    ) -> list[RetrievedChunk]:
        """Return the nearest chunks by cosine distance.

        Args:
            query_vector: Query vector, L2-normalised by the provider.
            limit: Maximum number of chunks.
            provider: When given, only vectors from this provider are considered.

        Returns:
            Chunks ordered by ascending distance, i.e. descending similarity.

        Raises:
            DependencyUnavailableError: If the database is unreachable.

        """
        sql = """
            SELECT c.id, c.document_id, d.title AS document_title, d.source,
                   c.section, c.position, c.text,
                   1 - (c.embedding <=> %(query)s::vector) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE (%(provider)s::text IS NULL OR c.provider = %(provider)s)
            ORDER BY c.embedding <=> %(query)s::vector
            LIMIT %(limit)s
        """
        params = {
            "query": "[" + ",".join(f"{value:.8f}" for value in query_vector) + "]",
            "limit": max(1, limit),
            "provider": provider,
        }
        return await self._search(sql, params, score_field="similarity")

    async def search_lexical(self, *, query: str, limit: int) -> list[RetrievedChunk]:
        """Return the best-matching chunks by PostgreSQL full-text rank.

        Args:
            query: Free-text question.
            limit: Maximum number of chunks.

        Returns:
            Chunks ordered by descending rank, highest first.

        Raises:
            DependencyUnavailableError: If the database is unreachable.

        """
        sql = """
            SELECT c.id, c.document_id, d.title AS document_title, d.source,
                   c.section, c.position, c.text,
                   ts_rank(c.search_vector, websearch_to_tsquery('english', %(query)s)) AS rank
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.search_vector @@ websearch_to_tsquery('english', %(query)s)
            ORDER BY rank DESC, c.id
            LIMIT %(limit)s
        """
        params = {"query": query, "limit": max(1, limit)}
        return await self._search(sql, params, score_field="rank")

    async def _search(
        self, sql: str, params: dict[str, Any], *, score_field: str
    ) -> list[RetrievedChunk]:
        """Run a search query and shape the rows.

        Args:
            sql: Parameterised query.
            params: Query parameters.
            score_field: Column holding the score.

        Returns:
            Retrieved chunks, best first.

        Raises:
            DependencyUnavailableError: If the database is unreachable.

        """
        try:
            async with self._database.acquire() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - normalised into the error taxonomy
            raise translate_db_error(exc) from exc
        return [
            RetrievedChunk(chunk=_row_to_chunk(row), score=float(row[score_field])) for row in rows
        ]

    async def stats(self) -> KnowledgeStats:
        """Return corpus counters.

        Returns:
            Counts of documents and chunks, and total chunk characters.

        Raises:
            DependencyUnavailableError: If the database is unreachable.

        """
        sql = """
            SELECT (SELECT count(*) FROM documents) AS documents,
                   count(*) AS chunks,
                   coalesce(sum(length(c.text)), 0) AS characters
            FROM chunks c
        """
        try:
            async with self._database.acquire() as conn, conn.cursor() as cur:
                await cur.execute(sql)
                row = await cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - normalised into the error taxonomy
            raise translate_db_error(exc) from exc
        return KnowledgeStats(
            documents=int(row["documents"]),
            chunks=int(row["chunks"]),
            characters=int(row["characters"]),
        )

    async def delete_document(self, document_id: str) -> int:
        """Remove a document and its chunks.

        Args:
            document_id: Document to remove.

        Returns:
            Number of chunks removed.

        """
        try:
            async with self._database.acquire() as conn, conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM chunks WHERE document_id = %(id)s", {"id": document_id}
                )
                await cur.execute("DELETE FROM documents WHERE id = %(id)s", {"id": document_id})
        except Exception as exc:  # noqa: BLE001 - normalised into the error taxonomy
            raise translate_db_error(exc) from exc
        return 1

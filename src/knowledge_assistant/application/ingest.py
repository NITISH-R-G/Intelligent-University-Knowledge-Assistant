"""Corpus ingestion: documents in, retrievable vectors out.

The pipeline is deliberately boring and linear, because the interesting failures in an
ingestion pipeline are the ones where a stage is skipped or repeated:

    load -> parse -> chunk -> embed -> store

Two properties matter more than throughput and both are enforced here:

**Determinism.** The same corpus produces the same chunk ids, the same chunk boundaries and
the same vectors on every machine. Without it a citation is not reproducible and an evaluation
number means nothing.

**Replace, never accumulate.** Storing a document deletes its previous chunks first. An
ingestion that appends will cite, weeks later, a rule that was edited out of the document -
which is the single worst failure a knowledge assistant can have.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from knowledge_assistant.domain.errors import ValidationError
from knowledge_assistant.domain.knowledge import (
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_CHUNK_TARGET_CHARS,
    Chunk,
    Document,
    chunk_document,
    slugify,
)

__all__ = ["IngestCorpus", "load_document", "load_corpus", "IngestReport"]


def _parse_markdown(text: str) -> str:
    """Return the plain text of a Markdown document.

    A deliberately tiny parser: it drops the ATX heading markers and the surrounding blank
    lines, and flattens runs of blank lines. It does not handle images, tables or HTML,
    because the corpus does not contain them - a general Markdown parser is a dependency and
    a source of silent mangling, for no benefit here.

    Args:
        text: Raw Markdown.

    Returns:
        Plain text with the same line structure.

    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("#"):
            _, _, rest = stripped.partition(" ")
            lines.append(rest.strip())
            continue
        lines.append(line)
    collapsed: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            collapsed.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                collapsed.append("")
    return "\n".join(collapsed).strip()


def load_document(path: Path, *, root: Path | None = None) -> Document:
    """Load and parse one Markdown document.

    Args:
        path: File to read.
        root: Corpus root, used to build a stable source path.

    Returns:
        The parsed document, with a content hash for idempotent re-ingestion.

    Raises:
        ValidationError: If the file cannot be read or contains no text.

    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read corpus document at {path.name}"
        raise ValidationError(msg, detail={"path": path.name}) from exc

    text = _parse_markdown(raw)
    if not text:
        msg = f"corpus document at {path.name} contains no text"
        raise ValidationError(msg, detail={"path": path.name})

    identifier = slugify(path.stem)
    title = text.splitlines()[0].strip() if text.splitlines() else identifier
    source = path.name if root is None else path.relative_to(root).as_posix()
    return Document(
        id=identifier,
        title=title or identifier,
        source=source,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_corpus(directory: Path, *, pattern: str = "*.md") -> list[Document]:
    """Load every document in a directory, in a stable order.

    Args:
        directory: Directory to scan.
        pattern: Glob applied to files.

    Returns:
        Documents sorted by id, so ingestion order never depends on filesystem order.

    Raises:
        ValidationError: If the directory does not exist or contains no documents.

    """
    root = Path(directory)
    if not root.is_dir():
        msg = f"corpus directory does not exist: {root.name}"
        raise ValidationError(msg, detail={"directory": root.name})
    documents = [load_document(path, root=root) for path in sorted(root.glob(pattern))]
    if not documents:
        msg = f"corpus directory contains no {pattern} documents"
        raise ValidationError(msg, detail={"directory": root.name, "pattern": pattern})
    return documents


class IngestReport:
    """What one ingestion run did.

    Attributes:
        documents: Documents seen.
        chunks: Chunks written.
        characters: Total characters of chunk text.
        ids: Document ids processed, sorted.

    """

    __slots__ = ("documents", "chunks", "characters", "ids")

    def __init__(self, *, documents: int, chunks: int, characters: int, ids: Sequence[str]) -> None:
        """Build the report.

        Args:
            documents: Documents seen.
            chunks: Chunks written.
            characters: Total chunk characters.
            ids: Document ids processed.

        """
        self.documents = documents
        self.chunks = chunks
        self.characters = characters
        self.ids = list(ids)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary.

        Returns:
            The counters as a plain mapping.

        """
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "characters": self.characters,
            "ids": self.ids,
        }

    def __repr__(self) -> str:
        """Return a compact representation.

        Returns:
            A one-line summary.

        """
        return (
            f"IngestReport(documents={self.documents}, chunks={self.chunks}, "
            f"characters={self.characters})"
        )


class IngestCorpus:
    """Turns a directory of documents into stored, embedded, citable chunks."""

    __slots__ = ("_store", "_embedder", "_target_chars", "_overlap_chars")

    def __init__(
        self,
        store: Any,
        embedder: Any,
        *,
        target_chars: int = DEFAULT_CHUNK_TARGET_CHARS,
        overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
    ) -> None:
        """Build the use case.

        Args:
            store: Adapter implementing the vector-store port.
            embedder: Adapter implementing the embedding port.
            target_chars: Target chunk size.
            overlap_chars: Characters of overlap between adjacent chunks.

        """
        self._store = (
            store.with_provider(embedder.name) if hasattr(store, "with_provider") else store
        )
        self._embedder = embedder
        self._target_chars = target_chars
        self._overlap_chars = overlap_chars

    def chunk(self, document: Document) -> list[Chunk]:
        """Return the chunks for one document.

        Args:
            document: Document to split.

        Returns:
            Chunks in document order.

        """
        return chunk_document(
            document, target_chars=self._target_chars, overlap_chars=self._overlap_chars
        )

    async def ingest_documents(self, documents: Sequence[Document]) -> IngestReport:
        """Ingest a sequence of documents.

        Args:
            documents: Documents to ingest.

        Returns:
            A report of what was written.

        Raises:
            ValidationError: If a vector does not match the chunk count, which would mean the
                embedding provider and the store disagree about the schema.

        """
        written = 0
        characters = 0
        ids: list[str] = []
        for document in documents:
            await self._store.upsert_document(
                document_id=document.id,
                title=document.title,
                source=document.source,
                text=document.text,
                content_hash=document.content_hash,
            )
            chunks = self.chunk(document)
            vectors = self._embedder.embed_many([c.text for c in chunks])
            if len(vectors) != len(chunks):
                msg = "embedding provider returned a different number of vectors than chunks"
                raise ValidationError(msg, detail={"document_id": document.id})
            count = await self._store.upsert_chunks(
                document_id=document.id, chunks=chunks, vectors=vectors
            )
            written += count
            characters += sum(len(c.text) for c in chunks)
            ids.append(document.id)

        return IngestReport(
            documents=len(documents), chunks=written, characters=characters, ids=sorted(ids)
        )

    async def ingest_directory(self, directory: Path, *, pattern: str = "*.md") -> IngestReport:
        """Load and ingest every document in a directory.

        Args:
            directory: Corpus root.
            pattern: Glob applied to files.

        Returns:
            A report of what was written.

        """
        return await self.ingest_documents(load_corpus(directory, pattern=pattern))

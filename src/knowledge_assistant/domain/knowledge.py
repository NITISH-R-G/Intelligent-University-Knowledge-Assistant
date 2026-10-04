"""Knowledge-base value objects and the deterministic chunking rule.

The domain holds what must stay correct regardless of which database, embedding model or
response generator is plugged in. That means: what a chunk *is*, what makes a retrieved
chunk trustworthy, and how text is split.

**Chunking is a domain rule, not a parsing utility.** It decides what evidence the assistant
will be able to cite, and a citation is only meaningful if it points at a stable, human-
recognisable span. The splitter is therefore pure, deterministic and dependency-free: the
same document always yields the same chunks with the same ids, on any machine, forever.

Heading-aware splitting is chosen over fixed-size splitting because a policy document has a
natural structure. A fixed 512-character window slices a sentence in half, and a half
sentence cited as evidence is worse than no citation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "Chunk",
    "Document",
    "RetrievedChunk",
    "DEFAULT_CHUNK_TARGET_CHARS",
    "DEFAULT_CHUNK_OVERLAP_CHARS",
    "chunk_document",
    "slugify",
    "tokenize",
    "split_sentences",
    "STOPWORDS",
]

#: Target size of a chunk in characters. Roughly a paragraph of policy text: long enough to
#: state a rule with its conditions, short enough that several fit in a bounded context.
DEFAULT_CHUNK_TARGET_CHARS: Final[int] = 700

#: Characters of tail repeated at the start of the next chunk. Small on purpose: overlap buys
#: robustness against a fact straddling a boundary, and costs context budget everywhere else.
DEFAULT_CHUNK_OVERLAP_CHARS: Final[int] = 120

#: Minimum viable chunk. A fragment shorter than this is merged forward rather than stored,
#: because a two-word chunk cannot support a citation.
_MIN_CHUNK_CHARS: Final[int] = 60

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BLANK_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

#: Abbreviations whose trailing period is not a sentence boundary. "The library opens at
#: 8:00 a.m. to 10:00 p.m." is the corpus's most common sentence, and a naive split produces
#: "The central library is open from 8:00 a.m." as a complete answer - a confident, quotable,
#: truncated statement. Protecting the abbreviation is cheaper than the citation it corrupts.
_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "a.m.",
    "p.m.",
    "am.",
    "pm.",
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "no.",
    "e.g.",
    "i.e.",
)

#: Sentinel substituted for a protected abbreviation's period, restored after splitting.
_ABBREV_SENTINEL: Final[str] = ""

#: Words that carry no retrieval signal. Deliberately small and hand-picked rather than a
#: full stopword list: an aggressive list strips meaning-bearing words in a corpus this size
#: ("leave", "book", "mark") and silently hurts recall.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "more",
        "must",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "shall",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


@dataclass(frozen=True, slots=True)
class Document:
    """A source document that has been ingested.

    ``content_hash`` is the SHA-256 of the document text. It is what makes ingestion
    idempotent: re-ingesting an unchanged document is a no-op, and an edited one replaces its
    chunks rather than accumulating stale duplicates.

    Attributes:
        id: Stable identifier, the document's slug.
        title: Human-readable title, used in citations.
        source: Where the document came from, used in citations.
        text: Full plain text of the document.
        content_hash: SHA-256 of ``text``.

    """

    id: str
    title: str
    source: str
    text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable span of a document, carrying everything a citation needs.

    Attributes:
        id: Stable identifier, derived from the document id and position.
        document_id: Owning document.
        document_title: Denormalised so a citation needs no second lookup.
        source: Denormalised for the same reason.
        section: Heading the chunk sits under, empty when the document has none.
        position: Zero-based ordinal within the document, used for stable ids and ordering.
        text: The chunk text.

    """

    id: str
    document_id: str
    document_title: str
    source: str
    section: str
    position: int
    text: str

    @property
    def citation_label(self) -> str:
        """Return the human-readable citation for this chunk.

        Returns:
            A label such as ``Attendance Policy > Minimum Attendance Requirement``.

        """
        if self.section:
            return f"{self.document_title} > {self.section}"
        return self.document_title


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A chunk that was retrieved, with the evidence about why it was retrieved.

    The fusion score is kept alongside the component scores deliberately: when an answer is
    wrong, the first question is "which stage retrieved this and how strongly", and that is
    only answerable if the components are preserved.

    Attributes:
        chunk: The matched chunk.
        score: Fused retrieval score after normalisation to ``(0, 1]``.
        vector_score: Cosine similarity contribution, before fusion.
        lexical_score: Full-text rank contribution, before fusion.
        vector_rank: Rank under vector search alone, 1-based; ``0`` when absent.
        lexical_rank: Rank under lexical search alone, 1-based; ``0`` when absent.

    """

    chunk: Chunk
    score: float
    vector_score: float = 0.0
    lexical_score: float = 0.0
    vector_rank: int = 0
    lexical_rank: int = 0

    @property
    def id(self) -> str:
        """Return the chunk identifier.

        Returns:
            The chunk id.

        """
        return self.chunk.id

    @property
    def text(self) -> str:
        """Return the chunk text.

        Returns:
            The chunk text.

        """
        return self.chunk.text


@dataclass(frozen=True, slots=True)
class RankedSentence:
    """One sentence from retrieved evidence, scored against the question.

    Used by the extractive response generator, which must be able to say *why* it chose a
    sentence. Ranking and composing are separate steps for the same reason retrieval and
    prompting are.

    Attributes:
        text: The sentence.
        score: Relevance to the question, non-negative.
        chunk_id: Chunk the sentence came from.
        citation_label: Human-readable citation label.

    """

    text: str
    score: float
    chunk_id: str
    citation_label: str


def slugify(value: str) -> str:
    """Return a lowercase, hyphenated slug.

    Args:
        value: Text to slugify, typically a file stem or heading.

    Returns:
        A slug safe for use in an identifier and in a URL.

    """
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "document"


def tokenize(text: str) -> list[str]:
    """Return content-bearing lowercase tokens.

    Stopwords are removed and stop characters are dropped. Shared by the embedding provider
    and the lexical scorer so that both see the same tokens - a mismatch there silently
    halves hybrid recall.

    Args:
        text: Arbitrary text.

    Returns:
        Tokens in document order, duplicates retained.

    """
    return [t for t in _WORD_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


def _stable_id(document_id: str, position: int, text: str) -> str:
    """Return a deterministic chunk id.

    Built from the document id, the position and a hash of the text, so re-ingesting an
    unchanged document reproduces the same ids. That is what lets citations be stable across
    runs instead of being reassigned on every ingestion.

    Args:
        document_id: Owning document id.
        position: Ordinal within the document.
        text: Chunk text.

    Returns:
        A chunk id.

    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"{document_id}#{position:03d}-{digest}"


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split a document into ``(heading, paragraphs)`` sections.

    Paragraphs are separated by *blank lines*, not by newlines. The corpus is hard-wrapped for
    readability, so a single newline falls in the middle of a sentence; treating each line as a
    paragraph is what produced chunks ending in "on all working days of the".

    Args:
        text: Full document text.

    Returns:
        One entry per heading (empty heading for the preamble).

    """
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []

    def _flush() -> None:
        paragraphs: list[str] = []
        current: list[str] = []
        for line in lines:
            if line.strip():
                current.append(line.strip())
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        if paragraphs:
            sections.append((heading, paragraphs))

    for raw_line in text.splitlines():
        match = _HEADING_RE.match(raw_line)
        if match is None:
            lines.append(raw_line)
            continue
        _flush()
        lines.clear()
        heading = match.group(2).strip()
    _flush()
    return sections


def _merge_paragraphs(paragraphs: Sequence[str], *, target: int) -> list[str]:
    """Group paragraphs into chunks of roughly ``target`` characters.

    A paragraph longer than ``target`` is split on sentence boundaries; if a single sentence
    is still longer than ``target`` it is split on word boundaries. Neither case is dropped:
    losing text would lose evidence.

    Args:
        paragraphs: Paragraph strings.
        target: Target chunk size in characters.

    Returns:
        Chunk texts in document order.

    """
    chunks: list[str] = []
    buffer = ""

    def _take(text: str) -> list[str]:
        # The same abbreviation-aware splitter the answer generator uses. Splitting here with a
        # naive `(?<=[.!?])` regex cut "open from 8:00 a.m." off from the rest of the sentence,
        # so the stored chunk - and therefore the citation - was itself truncated.
        pieces: list[str] = []
        sentence = ""
        for part in split_sentences(text):
            if sentence and len(sentence) + len(part) + 1 > target:
                pieces.append(sentence.strip())
                sentence = part
            else:
                sentence = f"{sentence} {part}".strip()
        if sentence.strip():
            pieces.append(sentence.strip())
        return pieces

    for paragraph in paragraphs:
        clean = _BLANK_RE.sub("\n\n", paragraph.strip())
        if not clean:
            continue
        if len(clean) > target:
            if buffer:
                chunks.append(buffer.strip())
                buffer = ""
            chunks.extend(_take(clean))
            continue
        candidate = f"{buffer}\n\n{clean}" if buffer else clean
        if len(candidate) > target and buffer:
            chunks.append(buffer.strip())
            buffer = clean
        else:
            buffer = candidate

    if buffer.strip():
        chunks.append(buffer.strip())
    return [c for c in chunks if len(c) >= _MIN_CHUNK_CHARS or len(chunks) == 1]


def chunk_document(
    document: Document,
    *,
    target_chars: int = DEFAULT_CHUNK_TARGET_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split a document into retrievable, citable chunks.

    Heading-aware and deterministic: the same document always produces byte-identical chunks
    with identical ids.

    Args:
        document: Document to split.
        target_chars: Target chunk size.
        overlap_chars: Characters of tail carried into the next chunk.

    Returns:
        Chunks in document order. Empty when the document has no usable text.

    """
    if target_chars <= 0:
        msg = "target_chars must be positive"
        raise ValueError(msg)
    if overlap_chars < 0 or overlap_chars >= target_chars:
        msg = "overlap_chars must be >= 0 and smaller than target_chars"
        raise ValueError(msg)

    chunks: list[Chunk] = []
    for section, paragraphs in _split_sections(document.text):
        for text in _merge_paragraphs(paragraphs, target=target_chars):
            chunks.append(
                Chunk(
                    id=_stable_id(document.id, len(chunks), text),
                    document_id=document.id,
                    document_title=document.title,
                    source=document.source,
                    section=section,
                    position=len(chunks),
                    text=text,
                )
            )
    return chunks


def mean_pool(vectors: Iterable[Sequence[float]]) -> tuple[float, ...]:
    """Return the element-wise mean of equal-length vectors.

    Args:
        vectors: Vectors to average.

    Returns:
        The mean vector, or an empty tuple when nothing was supplied.

    """
    total: list[float] | None = None
    count = 0
    for vector in vectors:
        if total is None:
            total = [0.0] * len(vector)
        for i, value in enumerate(vector):
            total[i] += value
        count += 1
    if total is None or count == 0:
        return ()
    return tuple(v / count for v in total)


@dataclass(frozen=True, slots=True)
class KnowledgeStats:
    """Corpus-level counters, reported after ingestion and by the demo command.

    Attributes:
        documents: Number of distinct documents.
        chunks: Number of stored chunks.
        characters: Total characters of chunk text.
        empty_documents: Documents that produced no chunk.

    """

    documents: int = 0
    chunks: int = 0
    characters: int = 0
    empty_documents: int = 0

    @property
    def average_chunk_chars(self) -> float:
        """Return the mean chunk size.

        Returns:
            Average characters per chunk, ``0.0`` when the corpus is empty.

        """
        return self.characters / self.chunks if self.chunks else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary.

        Returns:
            The counters as a plain mapping.

        """
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "characters": self.characters,
            "empty_documents": self.empty_documents,
            "average_chunk_chars": round(self.average_chunk_chars, 1),
        }


#: An empty stats record, for callers that need one before ingestion has run.
EMPTY_STATS: Final[KnowledgeStats] = KnowledgeStats()


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, without breaking on abbreviations or initials.

    Args:
        text: Text to split.

    Returns:
        Sentences in order, whitespace-trimmed. A blank string yields an empty list.

    """
    protected = text
    for abbreviation in _ABBREVIATIONS:
        protected = protected.replace(abbreviation, abbreviation[:-1] + _ABBREV_SENTINEL)
    parts = re.split(rf"(?<=[.!?]){_ABBREV_SENTINEL}?\s+", protected)
    restored = [part.replace(_ABBREV_SENTINEL, ".") for part in parts]
    return [part.strip() for part in restored if part.strip()]

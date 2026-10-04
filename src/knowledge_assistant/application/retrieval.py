"""Retrieval, context building and prompt construction.

Three separate steps on purpose, because they fail differently and are debugged differently:

**Retrieval** finds candidate evidence. **Context building** decides what the model is allowed
to see. **Prompting** decides how it is presented. Merging them means a retrieval bug looks
like a prompting bug, and there is no way to show a user what was retrieved before the prompt
was built - which is the first thing you need when an answer cites the wrong rule.

**Hybrid retrieval is not optional here.** The production embedding provider is a local
hashing encoder with no representation of meaning, so on its own it retrieves the chunk with
the most similar *spelling*. Fusing it with PostgreSQL full-text search, which reasons over
stemmed terms and phrase structure, is what makes the MVP retrieval work on questions whose
wording differs from the document's. The two rankings are combined with **Reciprocal Rank
Fusion**, not with a score sum, because cosine similarity and ``ts_rank`` are on
incomparable scales and a weighted sum of incomparable scores is not a ranking.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from knowledge_assistant.domain.errors import ValidationError
from knowledge_assistant.domain.knowledge import RetrievedChunk, tokenize

__all__ = [
    "RetrieveKnowledge",
    "BuildContext",
    "ContextBlock",
    "BuildPrompt",
    "DEFAULT_TOP_K",
    "RRF_K",
]

#: Default number of chunks each search arm returns. Deliberately larger than the context the
#: builder will keep: fusion is where the good candidates are found, and truncation happens
#: once, at the context boundary, where it is visible.
DEFAULT_TOP_K: Final[int] = 8

#: RRF damping constant. 60 is the value from the original paper: large enough that a single
#: high rank does not dominate, small enough that rank differences still matter.
RRF_K: Final[int] = 60

#: Fraction of the score budget a chunk must reach to count as evidence. Retrieved but
#: irrelevant chunks are the failure mode this threshold exists to stop: without it, the
#: answer generator always has *something* to say, and says it.
MIN_EVIDENCE_SCORE: Final[float] = 0.30


class RetrieveKnowledge:
    """Finds candidate evidence for a question.

    Queries the vector store and the lexical index independently, then fuses the two rankings
    with Reciprocal Rank Fusion.
    """

    __slots__ = ("_vector_store", "_lexical", "_embedder", "_top_k")

    def __init__(
        self, vector_store: Any, lexical: Any, embedder: Any, *, top_k: int = DEFAULT_TOP_K
    ) -> None:
        """Build the use case.

        Args:
            vector_store: Adapter answering dense-vector search.
            lexical: Adapter answering full-text search.
            embedder: Adapter producing query vectors.
            top_k: Number of candidates each arm returns.

        """
        self._vector_store = vector_store
        self._lexical = lexical
        self._embedder = embedder
        self._top_k = top_k

    @property
    def top_k(self) -> int:
        """Return the per-arm candidate count.

        Returns:
            The configured top-k.

        """
        return self._top_k

    async def retrieve(self, question: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        """Return the fused, best-first evidence for a question.

        Args:
            question: The user's question.
            top_k: Override for the per-arm candidate count.

        Returns:
            Chunks ordered by fused relevance, best first.

        Raises:
            ValidationError: If the question is blank.

        """
        normalized = question.strip()
        if not normalized:
            msg = "question must not be blank"
            raise ValidationError(msg, detail={"field": "question"})

        limit = top_k or self._top_k
        query_vector = self._embedder.embed(normalized)

        vector_hits = await self._vector_store.search_vector(query_vector=query_vector, limit=limit)
        lexical_hits = await self._lexical.search_lexical(query=normalized, limit=limit)
        return self._fuse(vector_hits, lexical_hits)

    def _fuse(
        self, vector_hits: Sequence[RetrievedChunk], lexical_hits: Sequence[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """Combine two rankings into one with Reciprocal Rank Fusion.

        Args:
            vector_hits: Vector-arm results, best first.
            lexical_hits: Lexical-arm results, best first.

        Returns:
            Fused results, best first, with scores normalised to ``(0, 1]``.

        """
        fused: dict[str, RetrievedChunk] = {}
        contribution: dict[str, float] = {}

        for hits, use_vector in ((vector_hits, True), (lexical_hits, False)):
            for rank, hit in enumerate(hits, start=1):
                existing = fused.get(hit.id)
                if existing is None:
                    fused[hit.id] = hit
                    contribution[hit.id] = 0.0
                    existing = hit
                contribution[hit.id] += 1.0 / (RRF_K + rank)
                if use_vector:
                    fused[hit.id] = RetrievedChunk(
                        chunk=hit.chunk,
                        score=existing.score,
                        vector_score=hit.score,
                        lexical_score=existing.lexical_score,
                        vector_rank=rank,
                        lexical_rank=existing.lexical_rank,
                    )
                else:
                    fused[hit.id] = RetrievedChunk(
                        chunk=hit.chunk,
                        score=existing.score,
                        vector_score=existing.vector_score,
                        lexical_score=hit.score,
                        vector_rank=existing.vector_rank,
                        lexical_rank=rank,
                    )

        best = max(contribution.values(), default=0.0)
        return [
            RetrievedChunk(
                chunk=fused[chunk_id].chunk,
                score=round(weight / best, 6) if best else 0.0,
                vector_score=fused[chunk_id].vector_score,
                lexical_score=fused[chunk_id].lexical_score,
                vector_rank=fused[chunk_id].vector_rank,
                lexical_rank=fused[chunk_id].lexical_rank,
            )
            for chunk_id, weight in sorted(
                contribution.items(), key=lambda item: (-item[1], item[0])
            )
        ]


class ContextBlock:
    """One chunk admitted to the bounded context.

    Attributes:
        chunk: The retrieved chunk.
        rank: Position in the final context, 1-based.

    """

    __slots__ = ("chunk", "rank")

    def __init__(self, chunk: RetrievedChunk, rank: int) -> None:
        """Build a block.

        Args:
            chunk: The retrieved chunk.
            rank: 1-based position.

        """
        self.chunk = chunk
        self.rank = rank

    @property
    def text(self) -> str:
        """Return the chunk text.

        Returns:
            The chunk text.

        """
        return self.chunk.text

    @property
    def citation_label(self) -> str:
        """Return the citation label.

        Returns:
            A human-readable label naming the document and section.

        """
        return self.chunk.chunk.citation_label


class BuildContext:
    """Trims retrieved evidence to a bounded, ordered, de-duplicated context.

    Bounding matters more than it looks. Without a hard cap the top-k grows until the prompt
    stops fitting, and the failure is silent: the answer gets worse as the corpus gets bigger,
    which looks like a model problem rather than a retrieval one.
    """

    __slots__ = ("_max_chunks", "_max_chars", "_min_score")

    def __init__(
        self, *, max_chunks: int = 4, max_chars: int = 4_000, min_score: float = MIN_EVIDENCE_SCORE
    ) -> None:
        """Build the context builder.

        Args:
            max_chunks: Hard cap on how many chunks enter the context.
            max_chars: Hard cap on total context characters.
            min_score: Fused score below which a chunk is treated as no evidence.

        Raises:
            ValueError: If a bound is not positive.

        """
        if max_chunks <= 0 or max_chars <= 0:
            msg = "context bounds must be positive"
            raise ValueError(msg)
        self._max_chunks = max_chunks
        self._max_chars = max_chars
        self._min_score = min_score

    def build(self, retrieved: Sequence[RetrievedChunk]) -> list[ContextBlock]:
        """Return the bounded context for a set of retrieved chunks.

        Ordering is by fused score, ties broken by chunk id, so the context is identical
        across runs. Chunks below ``min_score`` are dropped - that is what turns "we found
        something" into "we have evidence".

        Args:
            retrieved: Fused retrieval results.

        Returns:
            Context blocks in order. Empty when nothing cleared the evidence threshold.

        """
        seen_texts: set[str] = set()
        blocks: list[ContextBlock] = []
        used = 0
        for chunk in sorted(retrieved, key=lambda hit: (-hit.score, hit.id)):
            if len(blocks) >= self._max_chunks:
                break
            if chunk.score < self._min_score:
                break
            fingerprint = re.sub(r"\W+", " ", chunk.text.lower()).strip()
            if fingerprint in seen_texts:
                continue
            if used + len(chunk.text) > self._max_chars and blocks:
                break
            seen_texts.add(fingerprint)
            used += len(chunk.text)
            blocks.append(ContextBlock(chunk, len(blocks) + 1))
        return blocks


SYSTEM_INSTRUCTION: Final[str] = (
    "You are a university knowledge assistant. Answer only from the numbered context "
    "supplied below.\n"
    "Rules:\n"
    "1. Do not invent facts. If the context does not contain the answer, say so plainly.\n"
    "2. Cite the context item numbers you used, like [1].\n"
    "3. Distinguish what the context states from what it does not cover.\n"
    "4. Be concise and factual. Do not speculate about university policy."
)


class BuildPrompt:
    """Assembles the final prompt from a question and an already-bounded context.

    Kept separate from retrieval so the exact evidence handed to the generator can be shown,
    logged and asserted on. Item numbers are positional so a citation in the answer maps to
    a specific source the user can open.
    """

    __slots__ = ("_system",)

    def __init__(self, system_instruction: str = SYSTEM_INSTRUCTION) -> None:
        """Build the prompt builder.

        Args:
            system_instruction: The system instruction to use.

        """
        self._system = system_instruction

    def build(self, question: str, blocks: Sequence[ContextBlock]) -> str:
        """Return the assembled prompt.

        Args:
            question: The user's question.
            blocks: The bounded context.

        Returns:
            The full prompt, including an explicit statement when there is no context.

        """
        parts = [self._system, "", "CONTEXT:"]
        if blocks:
            for block in blocks:
                parts.append(f"[{block.rank}] {block.citation_label}\n{block.text.strip()}")
        else:
            parts.append("(no relevant context was retrieved)")
        parts.extend(["", f"QUESTION: {question.strip()}", "", "ANSWER:"])
        return "\n".join(parts)


def lexical_overlap(question: str, text: str) -> float:
    """Return the fraction of question content tokens present in ``text``.

    Used by the extractive generator to pick which sentence actually answers the question.
    Purely lexical, for the same reason the embedding provider is - and documented as such.

    Args:
        question: The question.
        text: Candidate sentence.

    Returns:
        A value in ``[0, 1]``.

    """
    wanted = set(tokenize(question))
    if not wanted:
        return 0.0
    present = wanted & set(tokenize(text))
    return len(present) / len(wanted)

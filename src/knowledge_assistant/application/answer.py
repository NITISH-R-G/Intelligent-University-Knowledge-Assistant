"""Response generation: turning retrieved evidence into a grounded answer.

**This is an MVP backend and it is labelled as one.** No language model is involved. The
generator picks the sentences from the retrieved context that best cover the question, joins
them, and cites them. That is a real, honest extractive system: every sentence in an answer is
a sentence that exists in a cited document. What it cannot do is paraphrase, synthesise across
documents, or answer a question the corpus does not literally contain.

It exists because the sprint has no local model runtime and downloading one is not free time,
and because an extractive baseline is the *right* MVP answer anyway: it makes hallucination
structurally impossible, which is the property a university knowledge assistant must have
before it has fluency.

:class:`ResponseGeneratorPort` is the seam. A local LLM implements ``generate`` and receives
the same already-bounded context, so swapping it changes no retrieval, prompt or citation code
- and cannot introduce evidence that retrieval did not supply.

**The refusal path is not a fallback, it is a feature.** When nothing clears the evidence
threshold the generator says it cannot answer. A generator that always produces an answer is
a hallucination engine with better manners.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from knowledge_assistant.application.retrieval import ContextBlock, lexical_overlap
from knowledge_assistant.domain.errors import ValidationError
from knowledge_assistant.domain.knowledge import RankedSentence, RetrievedChunk, split_sentences

__all__ = [
    "ExtractiveResponseGenerator",
    "INSUFFICIENT_EVIDENCE_ANSWER",
    "rank_sentences",
    "split_sentences",
]

#: Returned when the retrieved context does not clear the evidence threshold. Phrased so a
#: student can act on it rather than so it reads as a system failure.
INSUFFICIENT_EVIDENCE_ANSWER: str = (
    "I couldn't find enough information in the university knowledge base to answer that "
    "reliably. The documents I searched do not cover it. Try rephrasing the question, or "
    "contact the academic office."
)

#: Sentences shorter than this carry no answer content ("See below.").
_Final = str  # placeholder removed below

#: Hard cap on sentences quoted from any one chunk, so a long chunk cannot dominate.
_MAX_SENTENCES_PER_CHUNK: Final[int] = 2

#: Hard cap on sentences in one answer. Two or three sentences answer a policy question; a
#: paragraph does not, and a wall of quotes reads as the assistant failing to choose.
_MAX_SENTENCES: Final[int] = 3

#: Cap on total answer length, in characters.
_MAX_ANSWER_CHARS: Final[int] = 1_200

#: A module-level flag is not needed; this alias exists only so the two Final names below
#: read consistently with the section that uses them.

_MIN_SENTENCE_CHARS: Final[int] = 25

#: Paragraphs shorter than this, with no terminal punctuation, are section headings. The
#: corpus writes headings as bare title lines, and quoting one as if it were prose is how
#: "Minimum Attendance Requirement" ends up inside an answer as a fragment.
_MAX_HEADING_CHARS: Final[int] = 70


def _answerable_sentences(text: str) -> list[str]:
    """Return the sentences of a chunk that are long enough to quote.

    Delegates the split to the domain so the abbreviation handling lives in one place: a
    sentence boundary that the generator and the chunker disagree about is a citation that
    disagrees with the document.

    Args:
        text: Chunk text.

    Returns:
        Sentences long enough to carry an answer.

    """
    sentences: list[str] = []
    # Split on *blank* lines, not on single newlines. The corpus is hard-wrapped for
    # readability, so a single newline falls in the middle of a sentence and splitting on it
    # produced answers ending in "...on all working days of the". A blank line is the real
    # paragraph boundary.
    for paragraph in re.split(r"\n\s*\n", text):
        clean = " ".join(paragraph.split())
        if not clean:
            continue
        # Headings live in the chunk text and carry no terminal punctuation, so joining a
        # chunk into one line fuses "Minimum Attendance Requirement Every registered
        # student must..." into a single quoted sentence. A short paragraph with no sentence
        # punctuation is a heading; it is already carried by the citation label.
        if len(clean) < _MAX_HEADING_CHARS and clean[-1] not in ".?!":
            continue
        sentences.extend(
            part for part in split_sentences(clean) if len(part.strip()) >= _MIN_SENTENCE_CHARS
        )
    return sentences


def rank_sentences(question: str, blocks: Sequence[ContextBlock]) -> list[RankedSentence]:
    """Rank candidate sentences by how much of the question they cover.

    Args:
        question: The user's question.
        blocks: The bounded context.

    Returns:
        Ranked sentences, best first. Ties broken by position so the order is stable.

    """
    ranked: list[RankedSentence] = []
    for block in blocks:
        chunk: RetrievedChunk = block.chunk
        for index, sentence in enumerate(_answerable_sentences(chunk.text)):
            coverage = lexical_overlap(question, sentence)
            if coverage <= 0.0:
                continue
            # A mild preference for earlier sentences: in policy prose the opening sentence of
            # a section states the rule, and the rest elaborates.
            position_penalty = 1.0 - (0.05 * index)
            ranked.append(
                RankedSentence(
                    text=sentence,
                    score=round(coverage * position_penalty, 6),
                    chunk_id=chunk.id,
                    citation_label=block.citation_label,
                )
            )
    ranked.sort(key=lambda item: (-item.score, item.chunk_id, item.text))
    return ranked


class ExtractiveResponseGenerator:
    """Deterministic extractive answer composer.

    Implements :class:`ResponseGeneratorPort`. Produces an answer whose every sentence is a
    verbatim span of a retrieved chunk, with the chunk cited.
    """

    __slots__ = ("_name", "_min_coverage")

    def __init__(self, *, name: str = "mvp-extractive-v1", min_coverage: float = 0.30) -> None:
        """Build the generator.

        Args:
            name: Provider identifier recorded with each answer.
            min_coverage: Minimum fraction of the question's content tokens a sentence must
                cover to be quoted. This is the refusal threshold, and it is the most important
                number in the file. At 0.12 a question about "pets in the laboratory" was
                answered from a sentence containing only "laboratory" - confidently, with a
                citation, and about nothing. At 0.30 that sentence scores 0.20 and is refused,
                while a real answer scoring 0.33 or better still gets through.

        """
        self._name = name
        self._min_coverage = min_coverage

    @property
    def name(self) -> str:
        """Return the provider identifier.

        Returns:
            The generator name.

        """
        return self._name

    async def generate(self, *, question: str, context_chunks: Sequence[RetrievedChunk]) -> str:
        """Return an answer grounded in the supplied context.

        Args:
            question: The user's question.
            context_chunks: Retrieved evidence, already bounded by the context builder.

        Returns:
            An answer with inline citations, or the refusal sentence.

        """
        if not context_chunks:
            return INSUFFICIENT_EVIDENCE_ANSWER

        blocks = [ContextBlock(chunk, index + 1) for index, chunk in enumerate(context_chunks)]
        ranked = rank_sentences(question, blocks)
        usable = [item for item in ranked if item.score >= self._min_coverage]
        if not usable:
            return INSUFFICIENT_EVIDENCE_ANSWER

        per_chunk: dict[str, int] = {}
        chosen: list[RankedSentence] = []
        length = 0
        for item in usable:
            taken = per_chunk.get(item.chunk_id, 0)
            if taken >= _MAX_SENTENCES_PER_CHUNK:
                continue
            addition = len(item.text) + 6
            if length + addition > _MAX_ANSWER_CHARS and chosen:
                break
            if len(chosen) >= _MAX_SENTENCES:
                break
            per_chunk[item.chunk_id] = taken + 1
            length += addition
            chosen.append(item)

        if not chosen:
            return INSUFFICIENT_EVIDENCE_ANSWER

        body = " ".join(item.text for item in chosen)
        labels = _dedupe([item.citation_label for item in chosen])
        return f"{body}\n\nSources: {'; '.join(labels)}"


def _dedupe(values: Sequence[str]) -> list[str]:
    """Return ``values`` with duplicates removed, preserving first-seen order.

    Args:
        values: Labels to deduplicate.

    Returns:
        Unique labels in order.

    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class AnswerQuestion:
    """The end-to-end use case: question in, grounded answer and sources out.

    Retrieval, context building, prompting and generation are each injected, not constructed.
    That is what lets a test drive this whole path with a fixed store and a stub generator and
    still exercise the production context and prompt builders.
    """

    __slots__ = ("_retriever", "_context", "_prompt", "_generator", "_top_k")

    def __init__(
        self,
        retriever: Any,
        context_builder: Any,
        prompt_builder: Any,
        generator: Any,
        *,
        top_k: int | None = None,
    ) -> None:
        """Build the use case.

        Args:
            retriever: Retrieval use case.
            context_builder: Context builder.
            prompt_builder: Prompt builder.
            generator: Response generator.
            top_k: Optional retrieval depth override.

        """
        self._retriever = retriever
        self._context = context_builder
        self._prompt = prompt_builder
        self._generator = generator
        self._top_k = top_k

    async def answer(self, question: str) -> KnowledgeAnswer:
        """Answer a question from the knowledge base.

        Args:
            question: The user's question.

        Returns:
            The answer, its sources, and the intermediate retrieval trace.

        Raises:
            ValidationError: If the question is blank.

        """
        if not question.strip():
            msg = "question must not be blank"
            raise ValidationError(msg, detail={"field": "question"})

        retrieved = await self._retriever.retrieve(question, top_k=self._top_k)
        blocks = self._context.build(retrieved)
        prompt = self._prompt.build(question, blocks)
        admitted = [block.chunk for block in blocks]
        answer_text = await self._generator.generate(question=question, context_chunks=admitted)

        return KnowledgeAnswer(
            question=question.strip(),
            answer=answer_text,
            sources=[
                AnswerSource(
                    chunk_id=block.chunk.id,
                    document=block.chunk.chunk.document_title,
                    source=block.chunk.chunk.source,
                    section=block.chunk.chunk.section,
                    score=block.chunk.score,
                    rank=block.rank,
                    excerpt=block.text[:280],
                )
                for block in blocks
            ],
            prompt=prompt,
            retrieved=retrieved,
            grounded=bool(blocks) and answer_text != INSUFFICIENT_EVIDENCE_ANSWER,
            generator=self._generator.name,
        )


class AnswerSource:
    """A cited source for one answer.

    Attributes:
        chunk_id: Identifier of the cited chunk.
        document: Document title.
        source: Source file or path.
        section: Section within the document.
        score: Fused relevance score.
        rank: Position in the bounded context, 1-based.
        excerpt: Leading characters of the cited text, so the user can judge relevance
            without opening the document.

    """

    __slots__ = ("chunk_id", "document", "source", "section", "score", "rank", "excerpt")

    def __init__(
        self,
        *,
        chunk_id: str,
        document: str,
        source: str,
        section: str,
        score: float,
        rank: int,
        excerpt: str,
    ) -> None:
        """Build a source record.

        Args:
            chunk_id: Identifier of the cited chunk.
            document: Document title.
            source: Source file or path.
            section: Section within the document.
            score: Fused relevance score.
            rank: Position in the bounded context.
            excerpt: Leading characters of the cited text.

        """
        self.chunk_id = chunk_id
        self.document = document
        self.source = source
        self.section = section
        self.score = score
        self.rank = rank
        self.excerpt = excerpt

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation.

        Returns:
            The source as a plain mapping.

        """
        return {
            "chunk_id": self.chunk_id,
            "document": self.document,
            "source": self.source,
            "section": self.section,
            "score": self.score,
            "rank": self.rank,
            "excerpt": self.excerpt,
        }


class KnowledgeAnswer:
    """A complete answer with its evidence and the prompt that produced it.

    ``retrieved`` and ``prompt`` are retained deliberately: when an answer is wrong, the first
    question is whether retrieval or generation failed, and that is unanswerable unless both
    intermediates survive.

    Attributes:
        question: The question that was asked.
        answer: The answer text.
        sources: Admitted evidence, best first.
        prompt: The assembled prompt.
        retrieved: Everything fusion returned, including rejected candidates.
        grounded: Whether the answer is backed by quoted retrieved evidence. This is
            deliberately *not* the same as "context was non-empty": a query can retrieve
            chunks - "laboratory" really does match a question about pets in the laboratory -
            and still yield no sentence relevant enough to quote. Groundedness is a property of
            the answer, so it is measured on the answer.
        generator: Name of the backend that produced the answer.

    """

    __slots__ = ("question", "answer", "sources", "prompt", "retrieved", "grounded", "generator")

    def __init__(
        self,
        *,
        question: str,
        answer: str,
        sources: Sequence[AnswerSource],
        prompt: str,
        retrieved: Sequence[RetrievedChunk],
        grounded: bool,
        generator: str,
    ) -> None:
        """Build the answer record.

        Args:
            question: The question asked.
            answer: Answer text.
            sources: Admitted evidence.
            prompt: Assembled prompt.
            retrieved: Full fused results.
            grounded: Whether evidence cleared the threshold.
            generator: Backend identifier.

        """
        self.question = question
        self.answer = answer
        self.sources = list(sources)
        self.prompt = prompt
        self.retrieved = list(retrieved)
        self.grounded = grounded
        self.generator = generator

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for the HTTP API.

        Returns:
            The answer as a plain mapping.

        """
        return {
            "question": self.question,
            "answer": self.answer,
            "grounded": self.grounded,
            "generator": self.generator,
            "sources": [source.to_dict() for source in self.sources],
            "retrieved_count": len(self.retrieved),
            "cited_count": len(self.sources),
        }

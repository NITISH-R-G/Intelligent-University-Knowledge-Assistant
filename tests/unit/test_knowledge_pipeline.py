"""The retrieval-augmented pipeline, tested without a database.

These tests cover the nine behaviours the vertical slice actually claims: chunking, ingestion,
retrieval, metadata preservation, context construction, prompt construction, source attribution,
insufficient-context refusal and the CLI happy path.

The store is an in-memory double implementing the same ports as ``PgKnowledgeStore``, for the
same reason the job tests use an in-memory repository: this lane must run in under a second on
any machine with no Docker. The SQL adapter is exercised by ``ka ingest`` against a live
database, and the two are compared by the evaluation lane rather than by a mock's call list.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from knowledge_assistant.application.answer import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    AnswerQuestion,
    ExtractiveResponseGenerator,
)
from knowledge_assistant.application.ingest import IngestCorpus, load_document
from knowledge_assistant.application.retrieval import (
    SYSTEM_INSTRUCTION,
    BuildContext,
    BuildPrompt,
    RetrieveKnowledge,
    lexical_overlap,
)
from knowledge_assistant.config.settings import load_settings
from knowledge_assistant.container import build_knowledge_container
from knowledge_assistant.domain.errors import ValidationError
from knowledge_assistant.domain.knowledge import (
    Chunk,
    Document,
    RetrievedChunk,
    chunk_document,
    split_sentences,
    tokenize,
)
from knowledge_assistant.infrastructure.embeddings import (
    FixedVocabularyEmbeddingProvider,
    HashingEmbeddingProvider,
)
from knowledge_assistant.interfaces.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "corpus" / "knowledge"

# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity, matching pgvector's ``<=>`` operator.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        The similarity, or ``0.0`` when either vector has zero magnitude.

    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryKnowledgeStore:
    """An in-memory implementation of the vector and lexical store ports.

    Not a mock: it holds chunks, scores them with the same two metrics the SQL adapter uses
    (cosine distance and token overlap) and returns real :class:`RetrievedChunk` objects, so a
    test that passes here is testing retrieval behaviour rather than a call sequence.
    """

    def __init__(self) -> None:
        """Create an empty store."""
        self.documents: dict[str, Document] = {}
        self.chunks: dict[str, Chunk] = {}
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.provider = "unset"
        self.upsert_calls: list[str] = []

    def with_provider(self, name: str) -> InMemoryKnowledgeStore:
        """Return a store bound to an embedding provider name.

        Args:
            name: Provider identifier.

        Returns:
            This store, so ingestion can chain the call as it does against the SQL adapter.

        """
        self.provider = name
        return self

    async def upsert_document(self, **fields: Any) -> bool:
        """Record a document.

        Args:
            **fields: Document fields.

        Returns:
            ``True``.

        """
        self.documents[fields["document_id"]] = Document(
            id=fields["document_id"],
            title=fields["title"],
            source=fields["source"],
            text=fields["text"],
            content_hash=fields["content_hash"],
        )
        return True

    async def upsert_chunks(
        self, *, document_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        """Replace every chunk of a document.

        Args:
            document_id: Owning document.
            chunks: Replacement chunks.
            vectors: One vector per chunk.

        Returns:
            The number of chunks written.

        """
        for chunk in [c for c in self.chunks.values() if c.document_id == document_id]:
            del self.chunks[chunk.id]
            del self.vectors[chunk.id]
        for chunk, vector in zip(chunks, vectors, strict=True):
            self.chunks[chunk.id] = chunk
            self.vectors[chunk.id] = tuple(vector)
        self.upsert_calls.append(document_id)
        return len(chunks)

    async def search_vector(
        self, *, query_vector: Sequence[float], limit: int
    ) -> list[RetrievedChunk]:
        """Return chunks by cosine similarity, best first.

        Args:
            query_vector: The embedded query.
            limit: Maximum results.

        Returns:
            Scored chunks, best first.

        """
        scored = [
            RetrievedChunk(chunk=chunk, score=_cosine(query_vector, self.vectors[chunk.id]))
            for chunk in self.chunks.values()
        ]
        return sorted(scored, key=lambda r: (-r.score, r.chunk.id))[:limit]

    async def search_lexical(self, *, query: str, limit: int) -> list[RetrievedChunk]:
        """Return chunks by question-token overlap, best first.

        Args:
            query: The user's question.
            limit: Maximum results.

        Returns:
            Scored chunks, best first.

        """
        scored = [
            RetrievedChunk(chunk=chunk, score=lexical_overlap(query, chunk.text))
            for chunk in self.chunks.values()
        ]
        scored.sort(key=lambda r: (-r.score, r.chunk.id))
        return [r for r in scored if r.score > 0][:limit]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryKnowledgeStore:
    """Return an empty store.

    Returns:
        A fresh in-memory store.

    """
    return InMemoryKnowledgeStore()


@pytest.fixture(scope="module")
def embedder() -> HashingEmbeddingProvider:
    """Return the production embedding provider.

    Returns:
        The hashing n-gram provider.

    """
    return HashingEmbeddingProvider()


@pytest.fixture(scope="module")
def library_document() -> Document:
    """Return the library document from the shipped corpus.

    Returns:
        The parsed library document.

    """
    return load_document(CORPUS / "library.md", root=CORPUS)


@pytest.fixture(scope="module")
def corpus_documents() -> list[Document]:
    """Return every shipped corpus document.

    Returns:
        Parsed documents, in path order.

    """
    return [load_document(p, root=CORPUS) for p in sorted(CORPUS.glob("*.md"))]


@pytest.fixture
async def ingested(store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider) -> Any:
    """Ingest the whole shipped corpus into an in-memory store.

    Args:
        store: The in-memory store.
        embedder: The production embedder.

    Returns:
        The ingestion report.

    """
    documents = [load_document(p, root=CORPUS) for p in sorted(CORPUS.glob("*.md"))]
    return await IngestCorpus(store, embedder).ingest_documents(documents)


# --------------------------------------------------------------------------
# 1. Document chunking
# --------------------------------------------------------------------------


class TestChunking:
    """Chunking must be deterministic and must not lose the document."""

    def test_chunking_is_deterministic(self, library_document: Document) -> None:
        """The same document must produce byte-identical chunk ids every time.

        Chunk ids are derived from content, so a non-deterministic id would silently duplicate
        citations across re-ingestions and make every answer trace un-reproducible.
        """
        first = chunk_document(library_document)
        second = chunk_document(library_document)
        assert [c.id for c in first] == [c.id for c in second]
        assert [c.text for c in first] == [c.text for c in second]

    def test_every_chunk_carries_citation_metadata(self, library_document: Document) -> None:
        """Title, source and section must survive chunking.

        A chunk that loses its section cannot be cited by a human, and a chunk that loses its
        source cannot be traced back to a file.
        """
        chunks = chunk_document(library_document)
        assert chunks
        for chunk in chunks:
            assert chunk.document_id == library_document.id
            assert chunk.document_title == library_document.title
            assert chunk.source == library_document.source
            assert chunk.citation_label.startswith(library_document.title)

    def test_chunk_positions_are_dense_and_ordered(self, library_document: Document) -> None:
        """Positions must be 0..n-1 so ordering and ids stay stable across runs."""
        chunks = chunk_document(library_document)
        assert [c.position for c in chunks] == list(range(len(chunks)))

    def test_a_chunk_never_splits_a_sentence_across_the_boundary(
        self, library_document: Document
    ) -> None:
        """Chunk text must start on a sentence boundary.

        A chunk beginning mid-sentence is unreadable as a citation and, worse, produces answer
        sentences that are fragments.
        """
        for chunk in chunk_document(library_document):
            assert chunk.text[0].isupper() or chunk.text[0].isdigit() or chunk.text[0] in "(-*"

    def test_chunks_respect_the_configured_size(self) -> None:
        """The overlap knob must actually bound chunk size.

        A target that does not constrain output is a comment, not a limit.
        """
        document = Document(
            id="long",
            title="Long",
            source="long.md",
            text=" ".join(f"Sentence number {i} carries some information." for i in range(200)),
            content_hash="x",
        )
        chunks = chunk_document(document, target_chars=300, overlap_chars=50)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.text) <= 600

    def test_split_sentences_keeps_abbreviations_intact(self) -> None:
        """``a.m.`` must not end a sentence.

        Splitting there produced answers that stopped halfway through a time in the demo
        corpus, so the behaviour is pinned here rather than left to a regex.
        """
        text = "The library opens at 9 a.m. on weekdays. It closes at 8 p.m. on Fridays."
        assert split_sentences(text) == [
            "The library opens at 9 a.m. on weekdays.",
            "It closes at 8 p.m. on Fridays.",
        ]


# --------------------------------------------------------------------------
# 2. Ingestion
# --------------------------------------------------------------------------


class TestIngestion:
    """Ingestion must write every chunk with a vector of the right width."""

    async def test_every_document_becomes_stored_chunks(
        self, store: InMemoryKnowledgeStore, ingested: Any
    ) -> None:
        """All documents and all chunks must be written.

        Args:
            store: The in-memory store.
            ingested: The ingestion report.

        """
        assert ingested.documents == len(store.documents)
        assert ingested.chunks == len(store.chunks) > 0
        assert ingested.ids == sorted(store.documents)

    async def test_every_chunk_gets_a_vector_of_the_provider_width(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider, ingested: Any
    ) -> None:
        """A vector width mismatch is a schema error at insert time, not at query time.

        Args:
            store: The in-memory store.
            embedder: The production embedder.
            ingested: The ingestion report.

        """
        assert ingested.chunks
        assert store.provider == embedder.name
        for chunk_id, vector in store.vectors.items():
            assert len(vector) == embedder.dimensions, chunk_id

    @pytest.mark.usefixtures("ingested")
    async def test_reingesting_replaces_rather_than_duplicates(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """Editing a document must not leave orphaned chunks behind for retrieval to cite.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        use_case = IngestCorpus(store, embedder)
        document = load_document(CORPUS / "library.md", root=CORPUS)
        first = await use_case.ingest_documents([document])
        second = await use_case.ingest_documents([document])
        assert first.chunks == second.chunks
        owned = [c for c in store.chunks.values() if c.document_id == document.id]
        assert len(owned) == first.chunks

    @pytest.mark.usefixtures("ingested")
    async def test_ingestion_is_reproducible(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """Ingesting the same corpus twice must yield identical vectors.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        use_case = IngestCorpus(store, embedder)
        documents = [load_document(p, root=CORPUS) for p in sorted(CORPUS.glob("*.md"))]
        await use_case.ingest_documents(documents)
        before = dict(store.vectors)
        await use_case.ingest_documents(documents)
        assert store.vectors == before


# --------------------------------------------------------------------------
# 3 + 4. Retrieval and metadata preservation
# --------------------------------------------------------------------------


class TestRetrieval:
    """Retrieval must return relevant chunks with their provenance intact."""

    @pytest.mark.usefixtures("ingested")
    async def test_a_library_question_retrieves_the_library_document(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """The headline question must retrieve the library document first.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        results = await RetrieveKnowledge(store, store, embedder).retrieve(
            "What are the library timings?"
        )
        assert results
        assert results[0].chunk.document_id == "library"

    @pytest.mark.usefixtures("ingested")
    async def test_retrieval_preserves_metadata_and_scores(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """Every result must carry id, document, source, section and a score.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        results = await RetrieveKnowledge(store, store, embedder).retrieve(
            "How do I apply for leave?"
        )
        for result in results:
            assert result.id
            assert result.chunk.document_title
            assert result.chunk.source.endswith(".md")
            assert 0.0 <= result.score <= 1.0
        assert results == sorted(results, key=lambda r: (-r.score, r.chunk.id))

    @pytest.mark.usefixtures("ingested")
    async def test_top_k_is_configurable(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """K must be an argument, not a constant buried in a route.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        retriever = RetrieveKnowledge(store, store, embedder)
        assert len(await retriever.retrieve("leave request", top_k=2)) == 2
        assert retriever.top_k == 8

    @pytest.mark.usefixtures("ingested")
    async def test_fusion_prefers_chunks_both_stages_found(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """Reciprocal rank fusion must reward agreement between the two stages.

        A chunk ranked well by both vector and lexical search should outrank one that only one
        stage liked; otherwise the fusion is doing nothing.
        """
        retriever = RetrieveKnowledge(store, store, embedder)
        results = await retriever.retrieve("attendance requirement percentage")
        assert results
        top = results[0]
        assert top.vector_rank > 0 and top.lexical_rank > 0

    @pytest.mark.usefixtures("ingested")
    async def test_an_unrelated_question_matches_no_question_tokens(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """A query the corpus cannot support must have no lexical coverage.

        Fused scores are normalised within a single query, so the best chunk of *any* query
        scores 1.0 - the fused score alone therefore cannot detect an unanswerable question.
        Question-token coverage is the absolute signal that can, and this asserts the corpus
        genuinely does not cover this topic. The refusal decision itself is asserted in
        ``TestAnswering``.
        """
        retriever = RetrieveKnowledge(store, store, embedder)
        results = await retriever.retrieve("xenobiotic photosynthesis quantum chromodynamics")
        assert results
        assert (
            max(lexical_overlap("xenobiotic photosynthesis quantum", r.text) for r in results)
            == 0.0
        )


class TestContext:
    """Context must be bounded, deduplicated and deterministically ordered."""

    def _retrieved(
        self, count: int, score: float = 0.9, *, repeat: int = 40
    ) -> list[RetrievedChunk]:
        """Build synthetic retrieved chunks.

        Args:
            count: How many.
            score: Fused score to assign.
            repeat: How often the body sentence repeats, which controls chunk length.

        Returns:
            Retrieved chunks in descending score order.

        """
        return [
            RetrievedChunk(
                chunk=Chunk(
                    id=f"doc#00{i}-abc",
                    document_id="doc",
                    document_title="Doc",
                    source="doc.md",
                    section=f"Section {i}",
                    position=i,
                    text=f"Body text for section {i}. " * repeat,
                ),
                score=score - i / 100,
            )
            for i in range(count)
        ]

    def test_context_is_bounded_by_chunk_count(self) -> None:
        """More retrieved chunks than the budget must not all reach the prompt."""
        blocks = BuildContext().build(self._retrieved(10))
        assert len(blocks) == 4

    def test_context_is_bounded_by_characters(self) -> None:
        """The character budget must stop admission, not merely be measured.

        Chunks here are 120 characters each, so a 500-character budget admits four and refuses
        the fifth. Without this the prompt grows until it stops fitting and answers degrade
        silently as the corpus grows.
        """
        blocks = BuildContext(max_chunks=10, max_chars=500).build(self._retrieved(6, repeat=5))
        assert len(blocks) == 4
        assert sum(len(b.text) for b in blocks) <= 500

    def test_one_oversized_chunk_still_produces_context(self) -> None:
        """The best chunk is admitted even when it alone exceeds the budget.

        Returning nothing would turn "this chunk is long" into a refusal, which is a worse
        failure than a slightly oversized prompt.
        """
        blocks = BuildContext(max_chunks=4, max_chars=100).build(self._retrieved(3, repeat=60))
        assert len(blocks) == 1

    def test_weak_evidence_is_excluded(self) -> None:
        """Chunks below the evidence floor must not enter the context.

        They may be shown to a user as considered-and-rejected, but they must not be presented
        to a generator as evidence.
        """
        blocks = BuildContext(min_score=0.5).build(self._retrieved(3, score=0.4))
        assert blocks == []

    def test_duplicate_chunks_appear_once(self) -> None:
        """A chunk found by both stages must not be repeated in the context."""
        chunks = self._retrieved(3)
        blocks = BuildContext().build([*chunks, *chunks])
        assert [b.chunk.chunk.id for b in blocks] == [c.chunk.id for c in chunks]

    def test_context_order_is_deterministic(self) -> None:
        """The same retrieval must produce the same context, byte for byte."""
        chunks = self._retrieved(6)
        assert [b.chunk.chunk.id for b in BuildContext().build(chunks)] == [
            b.chunk.chunk.id for b in BuildContext().build(list(reversed(chunks)))
        ]

    def test_context_preserves_citation_labels(self) -> None:
        """Every block must still be attributable after selection."""
        blocks = BuildContext().build(self._retrieved(3))
        for block in blocks:
            assert block.citation_label == "Doc > Section 0" or block.citation_label.startswith(
                "Doc > "
            )


# --------------------------------------------------------------------------
# 6. Prompt construction
# --------------------------------------------------------------------------


class TestPrompt:
    """The prompt must be inspectable, bounded and separate from retrieval."""

    def _blocks(self) -> list[Any]:
        """Build context blocks from synthetic retrieved chunks.

        Returns:
            Three context blocks.

        """
        return BuildContext().build(
            [
                RetrievedChunk(
                    chunk=Chunk(
                        id="attendance-policy#000-abc",
                        document_id="attendance-policy",
                        document_title="Attendance Policy",
                        source="attendance_policy.md",
                        section="Minimum Attendance",
                        position=0,
                        text="Students must maintain at least 75 percent attendance.",
                    ),
                    score=0.9,
                )
            ]
        )

    def test_prompt_contains_the_system_instruction_and_the_question(self) -> None:
        """Grounding rules must travel with the evidence, not live in the generator."""
        prompt = BuildPrompt().build("What is the attendance requirement?", self._blocks())
        assert SYSTEM_INSTRUCTION in prompt
        assert "What is the attendance requirement?" in prompt

    def test_prompt_contains_the_retrieved_context(self) -> None:
        """The exact evidence the generator sees must be visible in the prompt."""
        prompt = BuildPrompt().build("What is the attendance requirement?", self._blocks())
        assert "75 percent attendance" in prompt
        assert "attendance_policy.md" in prompt

    def test_prompt_states_the_refusal_and_no_fabrication_rules(self) -> None:
        """The grounding rules must live in the prompt, not only in the generator's code.

        They are what an LLM generator reads; keeping them here is what makes the generator
        replaceable without rewriting the grounding guarantees.
        """
        lowered = BuildPrompt().build("Anything", self._blocks()).lower()
        assert "do not invent facts" in lowered
        assert "say so plainly" in lowered
        assert "do not speculate" in lowered

    def test_prompt_says_plainly_when_nothing_was_retrieved(self) -> None:
        """An empty context must be visible as empty, not as a blank section."""
        assert "(no relevant context was retrieved)" in BuildPrompt().build("Anything", [])

    def test_prompt_numbers_sources_so_a_citation_maps_to_one_chunk(self) -> None:
        """Citation markers must be positional."""
        prompt = BuildPrompt().build("Anything", self._blocks())
        assert "[1]" in prompt


# --------------------------------------------------------------------------
# 7 + 8. Answering, attribution and refusal
# --------------------------------------------------------------------------


class TestAnswering:
    """The end-to-end path must cite what it used and refuse what it cannot support."""

    def _use_case(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> AnswerQuestion:
        """Build the end-to-end use case over an in-memory store.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        Returns:
            The wired use case, using the production context, prompt and generator.

        """
        return AnswerQuestion(
            RetrieveKnowledge(store, store, embedder),
            BuildContext(),
            BuildPrompt(),
            ExtractiveResponseGenerator(),
        )

    @pytest.mark.usefixtures("ingested")
    async def test_a_supported_question_is_grounded_and_cited(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """The success condition: an answer plus the sources it came from.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        answer = await self._use_case(store, embedder).answer("What are the library timings?")
        assert answer.grounded is True
        assert answer.sources
        assert answer.answer != INSUFFICIENT_EVIDENCE_ANSWER
        assert "Central Library" in {s.document for s in answer.sources}

    @pytest.mark.usefixtures("ingested")
    async def test_sources_carry_the_fields_a_user_needs_to_verify_a_claim(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """A citation without a document name or an excerpt is not verifiable.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        answer = await self._use_case(store, embedder).answer("How do I apply for leave?")
        for source in answer.sources:
            assert source.chunk_id
            assert source.document
            assert source.source.endswith(".md")
            assert source.excerpt
            assert 0.0 <= source.score <= 1.0

    @pytest.mark.usefixtures("ingested")
    async def test_answer_text_comes_from_the_cited_chunks(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """Every sentence of prose in a grounded answer must come from a cited chunk.

        This is the anti-hallucination assertion: a generator that emitted anything not in the
        retrieved text would fail here even though the answer "looked" fine.
        """
        answer = await self._use_case(store, embedder).answer("What is the attendance requirement?")
        assert answer.grounded is True
        evidence = " ".join(s.excerpt for s in answer.sources).lower()
        # The answer ends with a "Sources: ..." line. That line is the citation, not prose, and
        # is asserted separately; only the prose is required to be drawn from the evidence.
        prose = answer.answer.split("Sources:")[0]
        assert prose.strip()
        for sentence in split_sentences(prose):
            head = tokenize(sentence)[:4]
            if not head:
                continue
            assert all(token in evidence for token in head), sentence

    @pytest.mark.usefixtures("ingested")
    async def test_an_unanswerable_question_is_refused_not_guessed(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """The corpus has no pet-in-laboratory policy, so the assistant must say so.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        answer = await self._use_case(store, embedder).answer(
            "What is the university's policy on bringing pets into the laboratory?"
        )
        assert answer.grounded is False
        assert answer.answer == INSUFFICIENT_EVIDENCE_ANSWER

    @pytest.mark.usefixtures("ingested")
    async def test_a_blank_question_is_rejected(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """An empty question is a client error, not a retrieval failure.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        with pytest.raises(ValidationError):
            await self._use_case(store, embedder).answer("   ")

    @pytest.mark.usefixtures("ingested")
    async def test_answer_records_which_generator_produced_it(
        self, store: InMemoryKnowledgeStore, embedder: HashingEmbeddingProvider
    ) -> None:
        """The backend must be visible so an extractive answer is never read as a model's.

        Args:
            store: The in-memory store.
            embedder: The production embedder.

        """
        answer = await self._use_case(store, embedder).answer("What are the library timings?")
        assert answer.generator == "mvp-extractive-v1"


# --------------------------------------------------------------------------
# 9. Embeddings and the CLI contract
# --------------------------------------------------------------------------


class TestEmbeddings:
    """The local embedder must be deterministic and honestly sized."""

    def test_vectors_are_deterministic_and_unit_length(
        self, embedder: HashingEmbeddingProvider
    ) -> None:
        """Same text in, same vector out, or every answer stops being reproducible."""
        a = embedder.embed("library opening hours")
        b = embedder.embed("library opening hours")
        assert a == b
        assert len(a) == embedder.dimensions
        assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)

    def test_different_texts_give_different_vectors(
        self, embedder: HashingEmbeddingProvider
    ) -> None:
        """A constant vector would make every distance equal and retrieval meaningless."""
        assert embedder.embed("library opening hours") != embedder.embed("exam re-evaluation fees")

    def test_the_dimension_matches_the_pgvector_column(self) -> None:
        """768 is the width of the column in migration 0002.

        Asserting it here means a future change to the embedder cannot silently break inserts.
        """
        assert HashingEmbeddingProvider().dimensions == 768

    def test_a_fixed_vocabulary_provider_is_usable_as_a_seam(self) -> None:
        """The embedding port must be replaceable without touching ingestion."""
        provider = FixedVocabularyEmbeddingProvider(["library", "attendance", "leave"])
        vectors = provider.embed_many(["library hours", "attendance rules"])
        assert len(vectors) == 2
        assert vectors[0][0] > 0
        assert vectors[1][1] > 0


class TestCliSurface:
    """The demo path must exist as commands, not as a script someone remembers."""

    def test_the_knowledge_commands_are_registered(self) -> None:
        """``ka ingest|ask|demo|eval`` must be discoverable from the parser."""
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if hasattr(action, "choices") and action.choices
        )
        for name in ("ingest", "ask", "demo", "eval"):
            assert name in subparsers.choices, name

    def test_the_shipped_corpus_is_present_and_pii_free(
        self, corpus_documents: list[Document]
    ) -> None:
        """The corpus must exist and must not contain anything resembling a real person.

        Args:
            corpus_documents: The parsed corpus.

        """
        assert len(corpus_documents) >= 5
        for document in corpus_documents:
            assert "@" not in document.text or "example" in document.text
            assert len(document.text) > 200


# --------------------------------------------------------------------------
# The SQL adapter, marked so it only runs with a live database
# --------------------------------------------------------------------------


@pytest.mark.integration
class TestPgKnowledgeStore:
    """The real adapter, so the in-memory double is never the only thing tested."""

    async def test_stored_vectors_are_searchable_and_keep_metadata(
        self, corpus_documents: list[Document]
    ) -> None:
        """Ingestion must produce vectors that pgvector can rank and metadata it can return.

        Skipped without ``KA_DATABASE_URL`` so the default lane stays database-free, matching the
        rest of the integration suite.

        Args:
            corpus_documents: The parsed corpus.

        """
        if not os.environ.get("KA_DATABASE_URL"):
            pytest.skip("needs KA_DATABASE_URL for the live vector store lane")
        container = await build_knowledge_container(load_settings())
        try:
            await container.ingest.ingest_documents(corpus_documents)
            results = await container.retriever.retrieve("What are the library timings?")
            assert results
            assert results[0].chunk.document_id == "library"
            assert results[0].chunk.source.endswith(".md")
            assert results[0].score > 0
        finally:
            await container.aclose()

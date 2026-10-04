"""The knowledge query endpoint.

One route: ``POST /api/v1/knowledge/query`` takes a question and returns an answer plus the
sources it was built from. This module is the *only* place HTTP meets the retrieval pipeline,
and it deliberately holds no retrieval logic - it maps a request onto
:class:`~knowledge_assistant.application.answer.AnswerQuestion` and maps the result back. Every
policy decision (what counts as evidence, how many chunks fit, when to refuse) lives in the
application layer, so changing the answer's behaviour never touches this file.

The route is opt-in. :func:`build_knowledge_router` is called by the application factory only
when a use case is supplied, so an app built for the foundation endpoints still serves no
business surface and its OpenAPI document is unchanged.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from knowledge_assistant.application.answer import KnowledgeAnswer

__all__ = [
    "KnowledgeQueryRequest",
    "KnowledgeQueryResponse",
    "KnowledgeSourceResponse",
    "KnowledgeQueryPort",
    "build_knowledge_router",
    "KNOWLEDGE_QUERY_PATH",
]

#: The single documented path. Named so the README, the tests and the route cannot drift.
KNOWLEDGE_QUERY_PATH = "/api/v1/knowledge/query"


class KnowledgeSourceResponse(BaseModel):
    """One cited source.

    Every field exists so a reader can verify the claim rather than trust it: ``document`` and
    ``source`` identify the file, ``section`` the passage within it, and ``excerpt`` the text
    itself. ``score`` is the retrieval score, which is relative to the other results of the same
    query and must not be read as a probability that the answer is correct.
    """

    document: str = Field(description="Human-readable document title.")
    source: str = Field(description="Path of the source file within the corpus.")
    chunk_id: str = Field(description="Stable identifier of the retrieved chunk.")
    section: str = Field(default="", description="Heading the chunk sits under, if any.")
    score: float = Field(description="Fused retrieval score, relative to other results.")
    rank: int = Field(ge=1, description="Position in the bounded context, 1-based.")
    excerpt: str = Field(description="The retrieved text, for verification.")


class KnowledgeQueryRequest(BaseModel):
    """A question for the knowledge base."""

    question: str = Field(
        min_length=1,
        max_length=2000,
        description="The user's question, in natural language.",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description="Retrieval depth override. Defaults to the configured value.",
    )


class KnowledgeQueryResponse(BaseModel):
    """A grounded answer and the evidence behind it.

    ``grounded`` is the field to branch on. When it is ``false`` the answer is a refusal, the
    sources are what was inspected rather than what was used, and the correct client behaviour is
    to tell the user the knowledge base does not cover the question.
    """

    question: str
    answer: str = Field(description="The answer, or a refusal when nothing could be grounded.")
    grounded: bool = Field(description="Whether the answer is supported by the sources.")
    sources: list[KnowledgeSourceResponse]
    generator: str = Field(description="Which response backend produced the answer.")
    retrieved_count: int = Field(ge=0, description="Chunks retrieved before context bounding.")
    prompt_chars: int = Field(
        ge=0,
        description="Characters in the assembled prompt, i.e. the evidence the generator saw.",
    )


@runtime_checkable
class KnowledgeQueryPort(Protocol):
    """What this module needs from the application layer.

    Declared here rather than importing ``AnswerQuestion`` into the signature so a test can pass
    a stub without constructing the whole retrieval stack. ``AnswerQuestion`` satisfies it.
    """

    async def answer(self, question: str) -> KnowledgeAnswer:
        """Return a grounded answer for ``question``.

        Args:
            question: The user's question.

        Returns:
            The answer, its sources and the retrieval trace.

        """


def _to_response(result: KnowledgeAnswer) -> KnowledgeQueryResponse:
    """Map an application result onto the response body.

    Args:
        result: The answer returned by the use case.

    Returns:
        The serialisable response.

    """
    return KnowledgeQueryResponse(
        question=result.question,
        answer=result.answer,
        grounded=result.grounded,
        generator=result.generator,
        retrieved_count=len(result.retrieved),
        prompt_chars=len(result.prompt),
        sources=[
            KnowledgeSourceResponse(
                document=source.document,
                source=source.source,
                chunk_id=source.chunk_id,
                section=source.section,
                score=source.score,
                rank=source.rank,
                excerpt=source.excerpt,
            )
            for source in result.sources
        ],
    )


def build_knowledge_router(answer: Any) -> APIRouter:
    """Build the knowledge query router.

    Args:
        answer: The end-to-end answering use case, or anything satisfying
            :class:`KnowledgeQueryPort`.

    Returns:
        An ``APIRouter`` exposing ``POST /api/v1/knowledge/query``.

    """
    router = APIRouter(tags=["knowledge"])

    @router.post(
        KNOWLEDGE_QUERY_PATH,
        response_model=KnowledgeQueryResponse,
        status_code=status.HTTP_200_OK,
        summary="Answer a question from the university knowledge base",
        responses={
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "description": "The question was empty or longer than the limit."
            }
        },
    )
    async def query(request: KnowledgeQueryRequest) -> KnowledgeQueryResponse:
        """Answer a question from retrieved, cited evidence.

        Args:
            request: The question.

        Returns:
            The answer, whether it is grounded, and its sources.

        """
        return _to_response(await answer.answer(request.question))

    return router

"""Local, deterministic, dependency-free embedding providers.

**What this is.** A signed random-projection encoder over character 4-grams and word
bigrams. Text is hashed into a fixed-width vector with sublinear term weighting and L2
normalisation. It runs in microseconds, needs no model download, no network and no GPU, and
produces byte-identical vectors on every machine and every run.

**What this is not.** It is not a semantic embedding, and calling it one would be dishonest.
It captures lexical and sub-word similarity - "attendance" and "attended" land near each other
because they share character 4-grams - but it has no representation of meaning at all. Two
sentences that mean the same thing with entirely different words are far apart in this space.
That is exactly why retrieval here fuses this vector search with PostgreSQL full-text search
rather than relying on either alone, and why the limitation is recorded on every stored vector
through the provider's ``name``.

**Why not download a real model.** A sentence-transformer would be a better MVP, but it is a
several-hundred-megabyte download, needs a working CUDA or ONNX runtime, and turns a two-hour
sprint into a download-waiting exercise with a binary dependency. The architecture does not
care which provider is wired: :class:`EmbeddingProviderPort` is the whole contract, and
swapping this for a real model changes one class and one ingestion run.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any, Final

from knowledge_assistant.domain.knowledge import tokenize

__all__ = [
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "HashingEmbeddingProvider",
    "FixedVocabularyEmbeddingProvider",
]

#: Vector width. Must equal the declared column width: pgvector *rejects* a shorter vector on
#: insert rather than padding it (padding only happens at comparison time), and an HNSW index
#: can only be built on a dimension-typed column. 768 buckets over a few thousand distinct
#: n-grams keeps collisions rare without wasting work.
DEFAULT_EMBEDDING_DIMENSIONS: Final[int] = 768

#: Character n-gram width. 4 is the shortest window that distinguishes policy vocabulary
#: ("attended"/"attendance") without being so long that unrelated words collide.
_NGRAM_WIDTH: Final[int] = 4

#: Weight applied to character n-grams relative to whole words. Below 1.0 because a policy
#: chunk is long and word identity carries more signal than sub-word shape.
_NGRAM_WEIGHT: Final[float] = 0.35

#: Weight applied to word bigrams. These are what make "leave request" rank above a chunk that
#: merely mentions both words in unrelated places.
_BIGRAM_WEIGHT: Final[float] = 0.55


def _hash_to_unit(token: str, seed: str) -> float:
    """Map a token to a reproducible value in ``(-1, 1)``.

    Args:
        token: Feature token.
        seed: Provider-specific seed, so two providers do not collide.

    Returns:
        A signed value in ``(-1, 1)``, non-zero.

    """
    digest = hashlib.sha256(f"{seed}:{token}".encode()).digest()
    # Take 8 bytes so the sign bit and the magnitude are independent.
    raw = int.from_bytes(digest[:8], "big")
    sign = 1.0 if raw & 1 else -1.0
    return sign * (((raw >> 1) / float((1 << 63) - 1)) or 1e-9)


def _l2_normalize(vector: list[float]) -> tuple[float, ...]:
    """Return the unit-length form of a vector.

    Args:
        vector: Vector to normalize.

    Returns:
        The unit vector, or an all-zero vector when the input has no magnitude.

    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


class HashingEmbeddingProvider:
    """Deterministic local encoder built from hashing, not from a trained model.

    Implements :class:`EmbeddingProviderPort`. Chosen for the MVP because it is free, instant,
    offline and reproducible - the four properties an evaluation set actually needs. See the
    module docstring for what it cannot do.
    """

    __slots__ = ("_dimensions", "_name", "_seed")

    def __init__(
        self,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        *,
        name: str = "local-hashing-ngram-v1",
        seed: str = "knowledge-assistant",
    ) -> None:
        """Build the provider.

        Args:
            dimensions: Vector width.
            name: Identifier recorded on every vector this provider produces.
            seed: Hash seed; changing it changes every vector.

        Raises:
            ValueError: If ``dimensions`` is not positive.

        """
        if dimensions <= 0:
            msg = "dimensions must be positive"
            raise ValueError(msg)
        self._dimensions = dimensions
        self._name = name
        self._seed = seed

    @property
    def dimensions(self) -> int:
        """Return the vector width.

        Returns:
            The configured dimension count.

        """
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the provider identifier.

        Returns:
            The provider name.

        """
        return self._name

    def embed(self, text: str) -> tuple[float, ...]:
        """Return the vector for one piece of text.

        Args:
            text: Text to encode. May be empty.

        Returns:
            A unit-length vector of :attr:`dimensions` floats.

        """
        vector = [0.0] * self._dimensions
        if not text or not text.strip():
            return tuple(vector)

        lowered = text.lower()
        words = tokenize(lowered)
        self._accumulate(vector, words, 1.0)
        self._accumulate(
            vector,
            [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)],
            _BIGRAM_WEIGHT,
        )
        padded = f"  {lowered}  "
        self._accumulate(
            vector,
            [padded[i : i + _NGRAM_WIDTH] for i in range(max(0, len(padded) - _NGRAM_WIDTH + 1))],
            _NGRAM_WEIGHT,
        )
        return _l2_normalize(vector)

    def _accumulate(self, vector: list[float], features: Sequence[str], weight: float) -> None:
        """Add weighted feature counts into a vector.

        Args:
            vector: Vector being built, mutated in place.
            features: Feature tokens.
            weight: Per-feature weight.

        """
        counts: dict[str, int] = {}
        for feature in features:
            counts[feature] = counts.get(feature, 0) + 1
        for feature, count in counts.items():
            # Sublinear term frequency: a word repeated twenty times should not dominate a
            # document that says twenty different relevant things.
            index_digest = hashlib.sha256(f"{self._seed}:idx:{feature}".encode()).digest()
            index = int.from_bytes(index_digest[:4], "big") % self._dimensions
            vector[index] += _hash_to_unit(feature, self._seed) * weight * (1.0 + math.log(count))

    def embed_many(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return vectors for a batch, in order.

        Args:
            texts: Texts to encode.

        Returns:
            One vector per input text.

        """
        return [self.embed(text) for text in texts]


class FixedVocabularyEmbeddingProvider:
    """A provider that maps a fixed vocabulary onto one dimension each.

    A test double, not a model. Useful where a test needs vectors that are trivially
    predictable - asserting that a query matching two of a document's words ranks that
    document first - without depending on hashing behaviour.
    """

    __slots__ = ("_dimensions", "_name", "_vocabulary")

    def __init__(self, vocabulary: Sequence[str], *, name: str = "fixed-vocabulary") -> None:
        """Build the provider.

        Args:
            vocabulary: Tokens, one dimension each.
            name: Provider identifier.

        Raises:
            ValueError: If the vocabulary is empty.

        """
        if not vocabulary:
            msg = "vocabulary must not be empty"
            raise ValueError(msg)
        self._vocabulary = {token.lower(): index for index, token in enumerate(vocabulary)}
        self._dimensions = len(self._vocabulary)
        self._name = name

    @property
    def dimensions(self) -> int:
        """Return the vector width.

        Returns:
            The vocabulary size.

        """
        return self._dimensions

    @property
    def name(self) -> str:
        """Return the provider identifier.

        Returns:
            The provider name.

        """
        return self._name

    def embed(self, text: str) -> tuple[float, ...]:
        """Return the indicator vector for one piece of text.

        Args:
            text: Text to encode.

        Returns:
            A unit-length indicator vector.

        """
        vector = [0.0] * self._dimensions
        for token in tokenize(text):
            index = self._vocabulary.get(token)
            if index is not None:
                vector[index] = 1.0
        return _l2_normalize(vector)

    def embed_many(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return vectors for a batch, in order.

        Args:
            texts: Texts to encode.

        Returns:
            One vector per input text.

        """
        return [self.embed(text) for text in texts]


def describe(provider: Any) -> dict[str, str | int]:
    """Return a small JSON-safe description of a provider.

    Args:
        provider: Any object satisfying :class:`EmbeddingProviderPort`.

    Returns:
        The provider name and dimension count.

    """
    return {"name": str(provider.name), "dimensions": int(provider.dimensions)}

"""
Embedding provider abstraction for the dense half of hybrid retrieval.

Production target (PRD Section 7) is Azure OpenAI embeddings — but that
needs live network + credentials this sandbox doesn't have (same
constraint as fetch_companyfacts() in Phase 1; see PROGRESS.md). Phase 2
is built and tested against TfidfEmbeddingProvider: genuine TF-IDF vectors
(not a fake/mocked return value), fit on the corpus being indexed — real
enough to prove the hybrid-index plumbing works, but it's a LEXICAL
representation, not semantic, so it will miss synonyms/paraphrases a real
embedding model would catch. Don't read retrieval-quality results from
this as representative of production quality.

The point of EmbeddingProvider as a shared interface: swapping in
AzureOpenAIEmbeddingProvider later should require no changes to
hybrid_index.py.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


class EmbeddingProvider(Protocol):
    dim: int

    def fit(self, corpus: list[str]) -> None:
        """Prepare against a corpus. No-op for providers (like Azure OpenAI)
        that don't need corpus-specific fitting."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class TfidfEmbeddingProvider:
    """Offline stand-in — see module docstring for what this is and isn't good for."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._vectorizer = TfidfVectorizer(max_features=dim)
        self._fitted = False

    def fit(self, corpus: list[str]) -> None:
        self._vectorizer.fit(corpus)
        self._fitted = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._fitted:
            raise RuntimeError("TfidfEmbeddingProvider.fit(corpus) must be called before embed().")
        matrix = self._vectorizer.transform(texts).toarray()
        if matrix.shape[1] < self.dim:
            # Pad to a fixed width so every vector has the same `dim`
            # regardless of vocabulary size — matters for small corpora
            # (like tests) where the fitted vocabulary is smaller than dim.
            pad = np.zeros((matrix.shape[0], self.dim - matrix.shape[1]))
            matrix = np.hstack([matrix, pad])
        return matrix.tolist()


class AzureOpenAIEmbeddingProvider:
    """
    Production provider per PRD Section 7. NOT implemented/tested here —
    this sandbox has no network path to Azure and no credentials. Stubbed
    so the interface exists and callers get a clear error rather than a
    silent wrong answer if selected without setup.
    """

    def __init__(self, dim: int = 1536):
        self.dim = dim

    def fit(self, corpus: list[str]) -> None:
        pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "AzureOpenAIEmbeddingProvider needs a real Azure OpenAI endpoint + "
            "credentials, not available in this sandbox. Implement against the "
            "Azure OpenAI SDK once you have those; it's a drop-in swap for "
            "TfidfEmbeddingProvider in hybrid_index.py, no other changes needed."
        )

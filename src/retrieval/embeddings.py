"""
Embedding providers for the dense half of hybrid retrieval.

Two real providers live here, plus the production Azure stub:

- SentenceTransformerEmbeddingProvider: genuine semantic embeddings from a
  local sentence-transformers model. This is the default for the `semantic`
  retrieval profile (see providers.py) and the one benchmark numbers should
  be read from.
- TfidfEmbeddingProvider: LEXICAL TF-IDF vectors. Real vectors, not a mock,
  but they match on shared tokens, not meaning, so they miss the synonyms and
  paraphrases a filing's prose is full of. Kept because it needs no model
  download, which keeps tests and CI hermetic. Do not read retrieval-quality
  results from it.

EmbeddingProvider is the shared interface: hybrid_index.py talks only to it,
so swapping providers changes no code there.

Queries and documents are encoded through separate entry points (`embed` for
documents, `embed_query` for queries) because instruction-tuned retrieval
models such as BGE expect a prefix on the query side only. Providers with no
such asymmetry just route embed_query back to embed.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# Instruction-tuned retrievers are trained with an asymmetric query prefix and
# lose accuracy without it. Matched on a prefix of the model name so the
# size and version variants all resolve.
_QUERY_PREFIXES = {
    "BAAI/bge": "Represent this sentence for searching relevant passages: ",
    "intfloat/e5": "query: ",
    "intfloat/multilingual-e5": "query: ",
}

_DOCUMENT_PREFIXES = {
    "intfloat/e5": "passage: ",
    "intfloat/multilingual-e5": "passage: ",
}


def _prefix_for(model_name: str, table: dict[str, str]) -> str:
    matches = [prefix for key, prefix in table.items() if model_name.lower().startswith(key.lower())]
    return max(matches, key=len) if matches else ""


@runtime_checkable
class EmbeddingProvider(Protocol):
    dim: int

    def fit(self, corpus: list[str]) -> None:
        """Prepare against a corpus. No-op for providers (like sentence-transformers
        or Azure OpenAI) that need no corpus-specific fitting."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode documents."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Encode a search query. Separate from embed() for asymmetric models."""
        ...


class TfidfEmbeddingProvider:
    """Lexical stand-in - see module docstring for what this is and isn't good for."""

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
            # regardless of vocabulary size - matters for small corpora
            # (like tests) where the fitted vocabulary is smaller than dim.
            pad = np.zeros((matrix.shape[0], self.dim - matrix.shape[1]))
            matrix = np.hstack([matrix, pad])
        return matrix.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class SentenceTransformerEmbeddingProvider:
    """
    Semantic embeddings from a local sentence-transformers model.

    The model loads lazily on first use, so constructing the provider is cheap
    and never touches the network; a cold cache downloads weights only once
    something actually encodes. `dim` is read from the loaded model rather than
    declared up front, so an index can never be created at a width the encoder
    does not produce.

    Pass `model=` to inject an already-loaded encoder (tests do this to stay
    offline); anything exposing `encode(list[str]) -> array` works.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        *,
        model=None,
        query_prefix: str | None = None,
        document_prefix: str | None = None,
        batch_size: int = 32,
        normalize: bool = True,
    ):
        self.model_name = model_name
        self.batch_size = batch_size
        self.normalize = normalize
        self.query_prefix = _prefix_for(model_name, _QUERY_PREFIXES) if query_prefix is None else query_prefix
        self.document_prefix = (
            _prefix_for(model_name, _DOCUMENT_PREFIXES) if document_prefix is None else document_prefix
        )
        self._model = model

    @property
    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "SentenceTransformerEmbeddingProvider needs the sentence-transformers "
                    "package. Install requirements.txt, or select the lexical retrieval "
                    "profile (RETRIEVAL_PROFILE=lexical) to run without it."
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def dim(self) -> int:
        # sentence-transformers 6 renamed get_sentence_embedding_dimension to
        # get_embedding_dimension; accept either, and fall back to measuring a
        # vector for encoders that report neither.
        for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            reader = getattr(self.model, name, None)
            if callable(reader):
                size = reader()
                if size is not None:
                    return int(size)
        return len(self._encode([""])[0])

    def fit(self, corpus: list[str]) -> None:
        """No-op: a pretrained encoder needs no corpus-specific fitting."""

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(vectors, dtype=float).tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encode([self.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.query_prefix + text])[0]


class AzureOpenAIEmbeddingProvider:
    """
    Production provider per PRD Section 7. NOT implemented here - no Azure
    endpoint or credentials are configured in this workspace (.env carries only
    SEC settings). Stubbed so the interface exists and callers get a clear
    error rather than a silent wrong answer if selected without setup.
    """

    def __init__(self, dim: int = 1536):
        self.dim = dim

    def fit(self, corpus: list[str]) -> None:
        pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "AzureOpenAIEmbeddingProvider needs a real Azure OpenAI endpoint + "
            "credentials, which are not configured here. Implement against the "
            "Azure OpenAI SDK once you have those; it is a drop-in swap for "
            "SentenceTransformerEmbeddingProvider, no other changes needed."
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def embed_query(provider: EmbeddingProvider, text: str) -> list[float]:
    """Encode a query through a provider that may predate embed_query()."""
    method = getattr(provider, "embed_query", None)
    if callable(method):
        return method(text)
    return provider.embed([text])[0]


def resolve_embedding_model_name() -> str:
    return os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)

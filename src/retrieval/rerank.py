"""
Reranking - Stage 2's last step per PRD v2 Section 5: "Cross-encoder
reranks top candidates before passing downstream."

Two rerankers, matching the two embedding providers:

- CrossEncoderReranker: a real sentence-transformers cross-encoder scoring
  each (query, passage) pair jointly. This is what Section 5 asks for and
  what the `semantic` retrieval profile uses.
- rerank_lexical_overlap: fraction of query terms present in the passage.
  Genuine scoring, not a mock, but weak - it cannot see a passage that
  answers the query in different words. Kept so tests and CI run with no
  model download.

Both satisfy the Reranker interface: (query, candidates, top_k) -> ranked
list, so callers swap one for the other with no other change.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Protocol, runtime_checkable

from src.retrieval.hybrid_index import RetrievedChunk

DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@runtime_checkable
class Reranker(Protocol):
    name: str

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
        ...


def rerank_lexical_overlap(query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
    """
    Scores each candidate by fraction of query terms present in its text.
    Deliberately simple - a lexical baseline, not a relevance model. See
    module docstring.
    """
    query_terms = set(query.lower().split())
    if not query_terms:
        return candidates[:top_k]

    def overlap_score(candidate: RetrievedChunk) -> float:
        candidate_terms = set(candidate.chunk.text.lower().split())
        return len(query_terms & candidate_terms) / len(query_terms)

    scored = [dataclasses.replace(c, rerank_score=overlap_score(c)) for c in candidates]
    scored.sort(key=lambda c: c.rerank_score, reverse=True)
    return scored[:top_k]


class LexicalOverlapReranker:
    """Object wrapper around rerank_lexical_overlap() so both rerankers share one interface."""

    name = "lexical_overlap"

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
        return rerank_lexical_overlap(query, candidates, top_k=top_k)


class CrossEncoderReranker:
    """
    Cross-encoder reranking with a local sentence-transformers model.

    A cross-encoder reads the query and the passage together, so unlike the
    bi-encoder that produced the dense candidates it can judge whether a
    passage actually answers this query rather than merely sitting near it in
    vector space. That is why it runs only over the fused top-k, never the
    whole corpus - it is far too slow for that.

    The model loads lazily on first rerank, so constructing this touches no
    network. Pass `model=` to inject a loaded encoder (tests do this);
    anything exposing `predict(list[tuple[str, str]]) -> scores` works.

    Ties keep the order the fusion stage produced, so a reranker that scores
    everything equally degrades to the hybrid ranking rather than shuffling it.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        *,
        model=None,
        batch_size: int = 16,
        max_candidates: int = 50,
    ):
        self.model_name = model_name
        self.name = f"cross_encoder:{model_name}"
        self.batch_size = batch_size
        self.max_candidates = max_candidates
        self._model = model

    @property
    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "CrossEncoderReranker needs the sentence-transformers package. "
                    "Install requirements.txt, or select the lexical retrieval "
                    "profile (RETRIEVAL_PROFILE=lexical) to run without it."
                ) from exc
            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
        if not candidates:
            return []
        pool = candidates[: self.max_candidates]
        scores = self.model.predict(
            [(query, c.chunk.text) for c in pool],
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        scored = [dataclasses.replace(c, rerank_score=float(s)) for c, s in zip(pool, scores)]
        order = sorted(range(len(scored)), key=lambda i: (-scored[i].rerank_score, i))
        return [scored[i] for i in order[:top_k]]


def resolve_reranker_model_name() -> str:
    return os.getenv("RERANKER_MODEL", DEFAULT_RERANKER_MODEL)

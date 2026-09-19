"""
Retrieval stack selection.

Path B has two interchangeable stacks, and which one produced a given result
matters enough to be recorded rather than inferred:

- `semantic` (default): sentence-transformers bi-encoder + cross-encoder
  reranker. PRD v2 Section 5 Stage 2 Path B describes this stack.
- `lexical`: TF-IDF vectors + query-term-overlap reranking. No model
  download, so tests, CI and the offline demo run without one. It matches on
  shared tokens rather than meaning, so retrieval-quality numbers measured on
  it are not the system's real numbers.

Selection order: explicit argument, then RETRIEVAL_PROFILE, then `semantic`.
An unknown profile raises. The semantic profile never silently degrades to
lexical when a model is missing - it raises with the opt-out spelled out,
because a quiet downgrade would put lexical numbers under a semantic label.

Every stack carries a `fingerprint`: the profile plus the exact model names.
Retrieval labels are only valid for the stack that produced the passages, so
the fingerprint is written into the review queue and the benchmark report and
compared on read. That is what stops a label set gathered under one stack
from being scored against another.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from src.retrieval.embeddings import (
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    TfidfEmbeddingProvider,
    resolve_embedding_model_name,
)
from src.retrieval.hybrid_index import RetrievedChunk
from src.retrieval.rerank import (
    CrossEncoderReranker,
    LexicalOverlapReranker,
    Reranker,
    resolve_reranker_model_name,
)

SEMANTIC = "semantic"
LEXICAL = "lexical"
PROFILES = (SEMANTIC, LEXICAL)
DEFAULT_PROFILE = SEMANTIC

ENV_PROFILE = "RETRIEVAL_PROFILE"


@dataclass(frozen=True)
class RetrievalStack:
    """An embedding provider and reranker chosen together, plus how to name them."""

    profile: str
    embeddings: EmbeddingProvider
    reranker: Reranker
    embedding_model: str
    reranker_model: str

    @property
    def fingerprint(self) -> str:
        return f"{self.profile}|{self.embedding_model}|{self.reranker_model}"

    def describe(self) -> str:
        if self.profile == LEXICAL:
            return ("Retrieval uses TF-IDF vectors and lexical overlap reranking, which match "
                    "shared words rather than meaning.")
        return (f"Retrieval uses {self.embedding_model} embeddings with {self.reranker_model} "
                "cross-encoder reranking.")

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
        return self.reranker.rerank(query, candidates, top_k=top_k)


def resolve_profile(profile: str | None = None) -> str:
    chosen = profile or os.getenv(ENV_PROFILE) or DEFAULT_PROFILE
    chosen = chosen.strip().lower()
    if chosen not in PROFILES:
        raise ValueError(f"Unknown retrieval profile {chosen!r}. Choose one of: {', '.join(PROFILES)}.")
    return chosen


def build_retrieval_stack(profile: str | None = None) -> RetrievalStack:
    """
    Construct the configured stack. Model weights are not loaded here - both
    providers load lazily on first use - so this stays cheap and offline.
    """
    chosen = resolve_profile(profile)
    if chosen == LEXICAL:
        return RetrievalStack(
            profile=LEXICAL,
            embeddings=TfidfEmbeddingProvider(),
            reranker=LexicalOverlapReranker(),
            embedding_model="tfidf",
            reranker_model="lexical_overlap",
        )
    embedding_model = resolve_embedding_model_name()
    reranker_model = resolve_reranker_model_name()
    return RetrievalStack(
        profile=SEMANTIC,
        embeddings=SentenceTransformerEmbeddingProvider(embedding_model),
        reranker=CrossEncoderReranker(reranker_model),
        embedding_model=embedding_model,
        reranker_model=reranker_model,
    )


def stack_from_fingerprint(fingerprint: str | None) -> RetrievalStack | None:
    """
    Rebuild the exact stack a fingerprint names, including its model names,
    ignoring current environment settings. Compiling a labeled queue has to
    evaluate under the stack that produced the labels, not whatever the
    environment happens to select now. Returns None for a queue with no
    recorded stack, so the caller can fall back to its own default.
    """
    if not fingerprint:
        return None
    parts = fingerprint.split("|")
    if len(parts) != 3:
        raise ValueError(f"Malformed retrieval stack fingerprint: {fingerprint!r}")
    profile, embedding_model, reranker_model = parts
    stack = build_retrieval_stack(profile)
    if profile == SEMANTIC:
        stack = RetrievalStack(
            profile=SEMANTIC,
            embeddings=SentenceTransformerEmbeddingProvider(embedding_model),
            reranker=CrossEncoderReranker(reranker_model),
            embedding_model=embedding_model,
            reranker_model=reranker_model,
        )
    if stack.fingerprint != fingerprint:
        raise ValueError(
            f"Cannot rebuild retrieval stack {fingerprint!r}; got {stack.fingerprint!r}."
        )
    return stack


def check_fingerprint(recorded: str | None, stack: RetrievalStack, what: str) -> None:
    """
    Raise if labels recorded under one retrieval stack are being used with
    another. Passages are ranked by the stack that retrieved them, so a
    relevance label does not carry across a stack change.
    """
    if recorded and recorded != stack.fingerprint:
        raise ValueError(
            f"{what} was produced by retrieval stack {recorded!r} but the current stack is "
            f"{stack.fingerprint!r}. Re-run the preparation step and re-label, or select the "
            f"original stack with {ENV_PROFILE}."
        )

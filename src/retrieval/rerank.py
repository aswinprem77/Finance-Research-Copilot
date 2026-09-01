"""
Reranking — Stage 2's last step per PRD v2 Section 5: "Cross-encoder
reranks top candidates before passing downstream."

Same offline constraint as embeddings.py: a real cross-encoder (e.g.
sentence-transformers CrossEncoder) needs a model downloaded from Hugging
Face Hub, which this sandbox can't reach. rerank_lexical_overlap() below is
a genuine-but-weak stand-in — real word-overlap scoring, not a mock — so
the "take hybrid results, rerank, return top N" pipeline shape is real and
tested even though the ranking quality itself isn't production-grade yet.
Swap in a real cross-encoder behind the same (query, candidates) -> ranked
list interface; nothing upstream needs to change.
"""
from __future__ import annotations

from src.retrieval.hybrid_index import RetrievedChunk


def rerank_lexical_overlap(query: str, candidates: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
    """
    Scores each candidate by fraction of query terms present in its text.
    Deliberately simple — this exists to prove the pipeline SHAPE (rerank
    step consumes hybrid results, returns a trimmed re-ordered list), not to
    be a real relevance model. See module docstring.
    """
    query_terms = set(query.lower().split())
    if not query_terms:
        return candidates[:top_k]

    def overlap_score(candidate: RetrievedChunk) -> float:
        candidate_terms = set(candidate.chunk.text.lower().split())
        return len(query_terms & candidate_terms) / len(query_terms)

    reranked = sorted(candidates, key=overlap_score, reverse=True)
    return reranked[:top_k]

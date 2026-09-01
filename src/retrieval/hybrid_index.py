"""
Hybrid retrieval index — Qdrant (dense) + BM25 (sparse), per PRD v2
Section 5 Stage 2 Path B. Runs Qdrant in local in-memory mode (no server
process, no network) — genuinely real vector search, just not persisted
to disk, which is the right tradeoff for dev/test and fine for
small-corpus use. Swap in a real Qdrant server URL for production; no
other code here changes.

Dense and BM25 scores live on different scales (cosine similarity vs. raw
BM25 term-weight sums), so they're merged via Reciprocal Rank Fusion (RRF)
— rank-based, not score-based, so it doesn't need score normalization
between two incomparable scales.
"""
from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from rank_bm25 import BM25Okapi

from src.retrieval.chunking import Chunk
from src.retrieval.embeddings import EmbeddingProvider

COLLECTION_NAME = "filing_chunks"
RRF_K = 60  # standard default for reciprocal rank fusion


@dataclass
class RetrievedChunk:
    chunk: Chunk
    dense_score: float | None
    bm25_score: float | None
    fused_score: float


class HybridIndex:
    def __init__(self, embedding_provider: EmbeddingProvider):
        self._embedder = embedding_provider
        self._client = QdrantClient(":memory:")
        self._chunks: list[Chunk] = []
        self._bm25: BM25Okapi | None = None
        self._built = False

    def build(self, chunks: list[Chunk]) -> None:
        if not chunks:
            raise ValueError("Cannot build an index over zero chunks.")
        self._chunks = chunks
        texts = [c.text for c in chunks]

        self._embedder.fit(texts)
        vectors = self._embedder.embed(texts)

        if self._client.collection_exists(COLLECTION_NAME):
            self._client.delete_collection(COLLECTION_NAME)
        self._client.create_collection(
            COLLECTION_NAME,
            vectors_config=VectorParams(size=self._embedder.dim, distance=Distance.COSINE),
        )
        self._client.upsert(
            COLLECTION_NAME,
            points=[PointStruct(id=i, vector=vectors[i], payload={}) for i in range(len(chunks))],
        )

        tokenized = [t.lower().split() for t in texts]
        self._bm25 = BM25Okapi(tokenized)
        self._built = True

    def search(self, query: str, top_k: int = 10) -> list[RetrievedChunk]:
        if not self._built:
            raise RuntimeError("HybridIndex.build(chunks) must be called before search().")

        query_vector = self._embedder.embed([query])[0]
        dense_hits = self._client.query_points(
            COLLECTION_NAME, query=query_vector, limit=len(self._chunks)
        ).points
        dense_rank = {hit.id: rank for rank, hit in enumerate(dense_hits)}
        dense_score_by_id = {hit.id: hit.score for hit in dense_hits}

        bm25_scores_all = self._bm25.get_scores(query.lower().split())
        bm25_order = sorted(range(len(bm25_scores_all)), key=lambda i: bm25_scores_all[i], reverse=True)
        bm25_rank = {doc_id: rank for rank, doc_id in enumerate(bm25_order)}

        fused_scores: dict[int, float] = {}
        for doc_id, rank in dense_rank.items():
            fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        for doc_id, rank in bm25_rank.items():
            fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)

        ranked_ids = sorted(fused_scores, key=lambda i: fused_scores[i], reverse=True)[:top_k]

        return [
            RetrievedChunk(
                chunk=self._chunks[doc_id],
                dense_score=dense_score_by_id.get(doc_id),
                bm25_score=float(bm25_scores_all[doc_id]) if doc_id < len(bm25_scores_all) else None,
                fused_score=fused_scores[doc_id],
            )
            for doc_id in ranked_ids
        ]

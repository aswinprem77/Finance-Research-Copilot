import pytest

from src.retrieval.chunking import Chunk
from src.retrieval.embeddings import TfidfEmbeddingProvider
from src.retrieval.hybrid_index import HybridIndex


def _sample_chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="c1", section="MD&A",
            text="Revenue increased due to strong automotive demand this quarter.",
            kind="prose", source_orders=[0],
        ),
        Chunk(
            chunk_id="c2", section="MD&A",
            text="Total revenue | $1,250,000 | $1,100,000",
            kind="table", source_orders=[1],
            raw_table=[["Total revenue", "$1,250,000", "$1,100,000"]],
        ),
        Chunk(
            chunk_id="c3", section="Risk Factors",
            text="A former supplier filed a lawsuit alleging breach of contract.",
            kind="prose", source_orders=[2],
        ),
        Chunk(
            chunk_id="c4", section="Risk Factors",
            text="Litigation outcomes are inherently uncertain and could materially affect results.",
            kind="prose", source_orders=[3],
        ),
    ]


def test_build_and_search_returns_relevant_chunk_first():
    index = HybridIndex(TfidfEmbeddingProvider(dim=32))
    index.build(_sample_chunks())
    results = index.search("lawsuit litigation supplier", top_k=2)
    assert results[0].chunk.chunk_id in ("c3", "c4")


def test_search_returns_scores_for_diagnosis():
    index = HybridIndex(TfidfEmbeddingProvider(dim=32))
    index.build(_sample_chunks())
    results = index.search("revenue automotive demand", top_k=4)
    assert len(results) > 0
    assert all(r.fused_score >= 0 for r in results)
    assert any(r.bm25_score is not None for r in results)
    assert any(r.dense_score is not None for r in results)


def test_build_rejects_empty_chunk_list():
    index = HybridIndex(TfidfEmbeddingProvider(dim=32))
    with pytest.raises(ValueError):
        index.build([])


def test_search_before_build_raises():
    index = HybridIndex(TfidfEmbeddingProvider(dim=32))
    with pytest.raises(RuntimeError):
        index.search("anything")


def test_top_k_is_respected():
    index = HybridIndex(TfidfEmbeddingProvider(dim=32))
    index.build(_sample_chunks())
    results = index.search("revenue", top_k=2)
    assert len(results) <= 2

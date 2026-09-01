from src.retrieval.chunking import Chunk
from src.retrieval.hybrid_index import RetrievedChunk
from src.retrieval.rerank import rerank_lexical_overlap


def _candidate(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(chunk_id=chunk_id, section="s", text=text, kind="prose", source_orders=[0]),
        dense_score=0.5,
        bm25_score=1.0,
        fused_score=0.02,
    )


def test_reranks_by_lexical_overlap():
    candidates = [
        _candidate("low", "completely unrelated content about factory equipment"),
        _candidate("high", "lawsuit litigation supplier breach of contract"),
    ]
    reranked = rerank_lexical_overlap("lawsuit litigation supplier", candidates, top_k=2)
    assert reranked[0].chunk.chunk_id == "high"


def test_reranks_respects_top_k():
    candidates = [_candidate(f"c{i}", "revenue automotive demand") for i in range(5)]
    reranked = rerank_lexical_overlap("revenue", candidates, top_k=2)
    assert len(reranked) == 2


def test_empty_query_returns_first_top_k_unchanged():
    candidates = [_candidate("a", "text one"), _candidate("b", "text two")]
    reranked = rerank_lexical_overlap("", candidates, top_k=1)
    assert reranked == candidates[:1]

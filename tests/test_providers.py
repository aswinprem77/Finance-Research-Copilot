"""
Tests for the semantic retrieval stack.

The real models are never downloaded here: both providers accept an injected
encoder, so these tests check the wiring (prefixes, dimensions, ordering,
fingerprints) offline and deterministically. One opt-in test at the bottom
loads real weights when explicitly enabled.
"""
import os

import numpy as np
import pytest

from src.retrieval.chunking import Chunk
from src.retrieval.embeddings import (
    SentenceTransformerEmbeddingProvider,
    TfidfEmbeddingProvider,
    embed_query,
)
from src.retrieval.hybrid_index import HybridIndex, RetrievedChunk
from src.retrieval.providers import (
    ENV_PROFILE,
    LEXICAL,
    SEMANTIC,
    build_retrieval_stack,
    check_fingerprint,
    resolve_profile,
    stack_from_fingerprint,
)
from src.retrieval.rerank import CrossEncoderReranker, LexicalOverlapReranker

BGE = "BAAI/bge-small-en-v1.5"


class FakeEncoder:
    """Stands in for a SentenceTransformer. Records exactly what it was asked to encode."""

    def __init__(self, dim: int = 4):
        self._dim = dim
        self.seen: list[str] = []

    def get_sentence_embedding_dimension(self):
        return self._dim

    def encode(self, texts, **kwargs):
        self.seen.extend(texts)
        # Deterministic, content-dependent vectors so different texts differ.
        return np.array([[float(len(t)), float(t.count("a")), 1.0, 0.0] for t in texts])


class FakeCrossEncoder:
    def __init__(self, scores):
        self.scores = scores
        self.pairs = None

    def predict(self, pairs, **kwargs):
        self.pairs = list(pairs)
        return self.scores[: len(self.pairs)]


def _candidate(chunk_id: str, text: str, fused: float = 0.02) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(chunk_id=chunk_id, section="s", text=text, kind="prose", source_orders=[0]),
        dense_score=0.5, bm25_score=1.0, fused_score=fused,
    )


# --- profile selection -------------------------------------------------------

def test_lexical_profile_builds_tfidf_stack():
    stack = build_retrieval_stack(LEXICAL)
    assert isinstance(stack.embeddings, TfidfEmbeddingProvider)
    assert isinstance(stack.reranker, LexicalOverlapReranker)
    assert stack.fingerprint == "lexical|tfidf|lexical_overlap"


def test_semantic_profile_builds_model_stack_without_loading_weights():
    stack = build_retrieval_stack(SEMANTIC)
    assert isinstance(stack.embeddings, SentenceTransformerEmbeddingProvider)
    assert isinstance(stack.reranker, CrossEncoderReranker)
    # Lazy: constructing the stack must not have loaded a model.
    assert stack.embeddings._model is None
    assert stack.reranker._model is None
    assert stack.fingerprint.startswith("semantic|")


def test_profile_comes_from_environment_when_unspecified(monkeypatch):
    monkeypatch.setenv(ENV_PROFILE, SEMANTIC)
    assert resolve_profile() == SEMANTIC
    # An explicit argument still wins over the environment.
    assert resolve_profile(LEXICAL) == LEXICAL


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown retrieval profile"):
        build_retrieval_stack("bm25-only")


def test_model_names_are_configurable(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/e5-small-v2")
    monkeypatch.setenv("RERANKER_MODEL", "cross-encoder/other")
    stack = build_retrieval_stack(SEMANTIC)
    assert stack.embedding_model == "intfloat/e5-small-v2"
    assert stack.reranker_model == "cross-encoder/other"
    assert stack.fingerprint == "semantic|intfloat/e5-small-v2|cross-encoder/other"


def test_describe_names_the_models_in_use():
    assert "TF-IDF" in build_retrieval_stack(LEXICAL).describe()
    assert BGE in build_retrieval_stack(SEMANTIC).describe()


# --- fingerprint guarding ----------------------------------------------------

def test_fingerprint_mismatch_is_refused():
    stack = build_retrieval_stack(LEXICAL)
    with pytest.raises(ValueError, match="retrieval stack"):
        check_fingerprint("semantic|BAAI/bge-small-en-v1.5|cross-encoder/x", stack, "Benchmark b")


def test_matching_or_absent_fingerprint_passes():
    stack = build_retrieval_stack(LEXICAL)
    check_fingerprint(stack.fingerprint, stack, "Benchmark b")
    check_fingerprint(None, stack, "Benchmark b")  # older queues predate the field


# --- embedding provider ------------------------------------------------------

def test_bge_prefixes_queries_but_not_documents():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider(BGE, model=encoder)
    provider.embed(["a passage about revenue"])
    provider.embed_query("revenue drivers")
    assert encoder.seen[0] == "a passage about revenue"
    assert encoder.seen[1] == provider.query_prefix + "revenue drivers"
    assert provider.query_prefix.startswith("Represent this sentence")


def test_e5_prefixes_both_sides():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider("intfloat/e5-small-v2", model=encoder)
    provider.embed(["some text"])
    provider.embed_query("some query")
    assert encoder.seen == ["passage: some text", "query: some query"]


def test_unknown_model_gets_no_prefix():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider("sentence-transformers/all-MiniLM-L6-v2", model=encoder)
    provider.embed_query("plain query")
    assert encoder.seen == ["plain query"]


def test_explicit_prefixes_override_the_model_defaults():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider(BGE, model=encoder, query_prefix="", document_prefix="doc: ")
    provider.embed(["x"])
    provider.embed_query("y")
    assert encoder.seen == ["doc: x", "y"]


def test_dim_is_read_from_the_model():
    provider = SentenceTransformerEmbeddingProvider(BGE, model=FakeEncoder(dim=7))
    assert provider.dim == 7


def test_dim_accepts_the_renamed_accessor():
    # sentence-transformers 6 renamed the method; both spellings must work.
    class RenamedEncoder(FakeEncoder):
        get_sentence_embedding_dimension = None

        def get_embedding_dimension(self):
            return self._dim

    assert SentenceTransformerEmbeddingProvider(BGE, model=RenamedEncoder(dim=9)).dim == 9


def test_dim_falls_back_to_measuring_a_vector():
    class SilentEncoder(FakeEncoder):
        get_sentence_embedding_dimension = None

    assert SentenceTransformerEmbeddingProvider(BGE, model=SilentEncoder()).dim == 4


def test_embedding_empty_document_list_is_a_no_op():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider(BGE, model=encoder)
    assert provider.embed([]) == []
    assert encoder.seen == []


def test_fit_is_a_no_op_for_a_pretrained_encoder():
    provider = SentenceTransformerEmbeddingProvider(BGE, model=FakeEncoder())
    provider.fit(["anything"])
    assert provider.embed(["x"])  # still usable, nothing raised


def test_embed_query_helper_falls_back_for_providers_without_the_method():
    class Legacy:
        dim = 2

        def fit(self, corpus):
            pass

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    assert embed_query(Legacy(), "q") == [1.0, 0.0]

    tfidf = TfidfEmbeddingProvider(dim=4)
    tfidf.fit(["revenue rose", "litigation risk"])
    assert embed_query(tfidf, "revenue") == tfidf.embed(["revenue"])[0]


def test_hybrid_index_uses_the_query_side_encoder():
    encoder = FakeEncoder()
    provider = SentenceTransformerEmbeddingProvider(BGE, model=encoder)
    index = HybridIndex(provider)
    try:
        index.build([_candidate("c1", "revenue rose").chunk, _candidate("c2", "litigation risk").chunk])
        encoder.seen.clear()
        index.search("revenue", top_k=1)
    finally:
        index.close()
    # The query went through embed_query (prefixed), not embed.
    assert encoder.seen == [provider.query_prefix + "revenue"]


# --- cross-encoder reranker --------------------------------------------------

def test_cross_encoder_orders_by_pair_score():
    model = FakeCrossEncoder([0.1, 0.9, 0.4])
    reranker = CrossEncoderReranker("cross-encoder/test", model=model)
    candidates = [_candidate("a", "one"), _candidate("b", "two"), _candidate("c", "three")]
    ranked = reranker.rerank("q", candidates, top_k=2)
    assert [r.chunk.chunk_id for r in ranked] == ["b", "c"]
    assert ranked[0].rerank_score == pytest.approx(0.9)
    # It scores the query against each passage jointly.
    assert model.pairs == [("q", "one"), ("q", "two"), ("q", "three")]


def test_cross_encoder_can_overturn_the_fused_ranking():
    # Fusion put "a" first; the cross-encoder disagrees. That is the point of the stage.
    model = FakeCrossEncoder([0.2, 0.8])
    reranker = CrossEncoderReranker("cross-encoder/test", model=model)
    candidates = [_candidate("a", "one", fused=0.9), _candidate("b", "two", fused=0.1)]
    assert [r.chunk.chunk_id for r in reranker.rerank("q", candidates, top_k=2)] == ["b", "a"]


def test_cross_encoder_ties_keep_the_fused_order():
    model = FakeCrossEncoder([0.5, 0.5, 0.5])
    reranker = CrossEncoderReranker("cross-encoder/test", model=model)
    candidates = [_candidate("a", "one"), _candidate("b", "two"), _candidate("c", "three")]
    assert [r.chunk.chunk_id for r in reranker.rerank("q", candidates, top_k=3)] == ["a", "b", "c"]


def test_cross_encoder_handles_no_candidates():
    reranker = CrossEncoderReranker("cross-encoder/test", model=FakeCrossEncoder([]))
    assert reranker.rerank("q", [], top_k=5) == []


def test_cross_encoder_caps_the_candidate_pool():
    model = FakeCrossEncoder([0.5] * 100)
    reranker = CrossEncoderReranker("cross-encoder/test", model=model, max_candidates=3)
    reranker.rerank("q", [_candidate(f"c{i}", "text") for i in range(10)], top_k=2)
    assert len(model.pairs) == 3


def test_lexical_reranker_records_its_score():
    ranked = LexicalOverlapReranker().rerank("lawsuit litigation", [
        _candidate("hit", "a lawsuit and litigation followed"),
        _candidate("miss", "unrelated factory equipment"),
    ], top_k=2)
    assert [r.chunk.chunk_id for r in ranked] == ["hit", "miss"]
    assert ranked[0].rerank_score == pytest.approx(1.0)
    assert ranked[1].rerank_score == pytest.approx(0.0)


# --- opt-in: real weights ----------------------------------------------------

@pytest.mark.skipif(
    os.getenv("RUN_MODEL_TESTS") != "1",
    reason="Downloads real model weights; set RUN_MODEL_TESTS=1 to run.",
)
def test_real_semantic_stack_beats_lexical_on_a_paraphrase():
    """
    The reason for this phase: a passage that answers the query in different
    words. Lexical scoring cannot see it; the semantic stack should.
    """
    chunks = [
        Chunk(chunk_id="noise", section="s", kind="prose", source_orders=[0],
              text="The company maintains offices in several countries and employs staff worldwide."),
        Chunk(chunk_id="target", section="s", kind="prose", source_orders=[1],
              text="A former supplier has initiated legal proceedings against us alleging breach of contract."),
    ]
    stack = build_retrieval_stack(SEMANTIC)
    index = HybridIndex(stack.embeddings)
    try:
        index.build(chunks)
        hits = stack.rerank("pending lawsuit filed against the company", index.search("pending lawsuit filed against the company"), top_k=1)
    finally:
        index.close()
    assert hits[0].chunk.chunk_id == "target"


# --- rebuilding a recorded stack ---------------------------------------------

def test_stack_rebuilt_from_fingerprint_ignores_the_environment(monkeypatch):
    # A queue labeled under one embedding model must be evaluated under that
    # model, not whatever EMBEDDING_MODEL currently says.
    monkeypatch.setenv("EMBEDDING_MODEL", "some/other-model")
    stack = stack_from_fingerprint("semantic|BAAI/bge-small-en-v1.5|cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert stack.embedding_model == BGE
    assert stack.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert stack.embeddings._model is None  # still lazy


def test_lexical_fingerprint_rebuilds_the_lexical_stack():
    stack = stack_from_fingerprint("lexical|tfidf|lexical_overlap")
    assert isinstance(stack.embeddings, TfidfEmbeddingProvider)


def test_absent_fingerprint_rebuilds_nothing():
    assert stack_from_fingerprint(None) is None
    assert stack_from_fingerprint("") is None


def test_malformed_fingerprint_is_rejected():
    with pytest.raises(ValueError, match="Malformed retrieval stack fingerprint"):
        stack_from_fingerprint("semantic|only-two-parts")


def test_fingerprint_with_mismatched_lexical_models_is_rejected():
    with pytest.raises(ValueError, match="Cannot rebuild retrieval stack"):
        stack_from_fingerprint("lexical|BAAI/bge-small-en-v1.5|lexical_overlap")

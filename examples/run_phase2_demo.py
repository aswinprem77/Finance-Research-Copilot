"""
Phase 2 demo — runs the full Path B pipeline end-to-end against the
synthetic filing fixture: parse HTML -> chunk -> build hybrid index ->
search -> rerank, plus HTML-table fallback fact extraction.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so `src.*` resolves when run directly

from src.retrieval.chunking import chunk_blocks
from src.retrieval.embeddings import TfidfEmbeddingProvider
from src.retrieval.hybrid_index import HybridIndex
from src.retrieval.html_ingest import TableBlock, parse_filing_html
from src.retrieval.rerank import rerank_lexical_overlap
from src.retrieval.table_fallback import extract_facts_from_table
from src.schema.financial_schema import FiscalPeriod

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_filing_excerpt.html"


def main() -> None:
    html = FIXTURE_PATH.read_text()

    blocks = parse_filing_html(html)
    print(f"Parsed {len(blocks)} blocks from the filing excerpt.\n")

    chunks = chunk_blocks(blocks)
    print(f"Chunked into {len(chunks)} chunks:")
    for c in chunks:
        preview = c.text[:120].replace("\n", " ")
        print(f"  [{c.kind:5s}] ({c.section[:45]}) {preview}...")
    print()

    index = HybridIndex(TfidfEmbeddingProvider(dim=64))
    index.build(chunks)

    query = "lawsuit litigation supplier breach of contract"
    print(f"-- Hybrid search: {query!r} --")
    hits = index.search(query, top_k=3)
    for r in hits:
        print(f"  fused={r.fused_score:.4f}  bm25={r.bm25_score:.2f}  [{r.chunk.kind}] {r.chunk.text[:120]}...")

    reranked = rerank_lexical_overlap(query, hits, top_k=2)
    print("\n-- After rerank --")
    for r in reranked:
        print(f"  [{r.chunk.kind}] {r.chunk.text[:120]}...")

    print("\n-- HTML-table fallback extraction --")
    table = next(b for b in blocks if isinstance(b, TableBlock))
    facts = extract_facts_from_table(
        table, company_cik="9999999", fiscal_year=2024, fiscal_period=FiscalPeriod.Q2,
        period_end_date=date(2024, 6, 30),
    )
    for f in facts:
        print(f"  {f.concept.value}: ${f.value:,.0f} (source={f.source.value}, tag={f.source_tag})")


if __name__ == "__main__":
    main()

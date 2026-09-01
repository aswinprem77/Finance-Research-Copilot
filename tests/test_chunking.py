from pathlib import Path

from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"


def _load_blocks():
    return parse_filing_html(FIXTURE_PATH.read_text())


def test_table_is_always_its_own_chunk():
    chunks = chunk_blocks(_load_blocks())
    table_chunks = [c for c in chunks if c.kind == "table"]
    assert len(table_chunks) == 1
    assert table_chunks[0].raw_table is not None
    assert any(row[0] == "Total revenue" for row in table_chunks[0].raw_table)


def test_prose_never_merges_across_section_boundary():
    chunks = chunk_blocks(_load_blocks())
    mda_chunks = [c for c in chunks if "Management's Discussion" in c.section]
    risk_chunks = [c for c in chunks if "Risk Factors" in c.section]
    assert mda_chunks and risk_chunks
    for c in mda_chunks:
        assert "former supplier filed a complaint" not in c.text


def test_small_max_chunk_chars_splits_prose_into_more_chunks():
    blocks = _load_blocks()
    chunks_default = chunk_blocks(blocks)
    chunks_tiny = chunk_blocks(blocks, max_chunk_chars=40)
    prose_default = [c for c in chunks_default if c.kind == "prose"]
    prose_tiny = [c for c in chunks_tiny if c.kind == "prose"]
    assert len(prose_tiny) > len(prose_default)


def test_table_flattening_keeps_label_next_to_value():
    chunks = chunk_blocks(_load_blocks())
    table_chunk = next(c for c in chunks if c.kind == "table")
    assert "Total revenue | $1,250,000" in table_chunk.text


def test_every_chunk_has_nonempty_section():
    chunks = chunk_blocks(_load_blocks())
    assert all(c.section for c in chunks)

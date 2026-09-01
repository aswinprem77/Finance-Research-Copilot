from pathlib import Path

from src.retrieval.html_ingest import ProseBlock, TableBlock, parse_filing_html

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"


def _load_html() -> str:
    return FIXTURE_PATH.read_text()


def test_parses_headings_into_sections():
    blocks = parse_filing_html(_load_html())
    sections = {b.section for b in blocks}
    assert any("Management's Discussion" in s for s in sections)
    assert any("Risk Factors" in s for s in sections)


def test_separates_prose_and_table_blocks():
    blocks = parse_filing_html(_load_html())
    prose = [b for b in blocks if isinstance(b, ProseBlock)]
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1
    assert len(prose) >= 4


def test_table_block_captures_rows_and_caption():
    blocks = parse_filing_html(_load_html())
    table = next(b for b in blocks if isinstance(b, TableBlock))
    assert table.caption is not None and "selected results" in table.caption.lower()
    revenue_row = next(r for r in table.rows if r[0] == "Total revenue")
    assert revenue_row[1] == "$1,250,000"
    assert revenue_row[2] == "$1,100,000"


def test_document_order_preserved():
    blocks = parse_filing_html(_load_html())
    orders = [b.order for b in blocks]
    assert orders == sorted(orders)


def test_table_rows_no_p_tags_leaked_from_inside_table():
    # None of our fixture's <td> cells contain nested <p> tags, but this
    # guards the "skip elements nested inside table" logic in the parser
    # against ever double-counting table content as a separate ProseBlock.
    blocks = parse_filing_html(_load_html())
    table = next(b for b in blocks if isinstance(b, TableBlock))
    prose_texts = [b.text for b in blocks if isinstance(b, ProseBlock)]
    for row in table.rows:
        row_text = " ".join(row)
        assert row_text not in prose_texts

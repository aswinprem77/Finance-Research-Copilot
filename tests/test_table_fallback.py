from datetime import date
from pathlib import Path

from src.retrieval.html_ingest import TableBlock, parse_filing_html
from src.retrieval.table_fallback import extract_facts_from_table
from src.schema.financial_schema import FactSource, FinancialConcept, FiscalPeriod

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"


def _get_table_block() -> TableBlock:
    blocks = parse_filing_html(FIXTURE_PATH.read_text())
    return next(b for b in blocks if isinstance(b, TableBlock))


def _extract():
    return extract_facts_from_table(
        _get_table_block(),
        company_cik="9999999",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.Q2,
        period_end_date=date(2024, 6, 30),
    )


def test_extracts_revenue_and_net_income():
    facts = _extract()
    revenue = next(f for f in facts if f.concept == FinancialConcept.REVENUE)
    assert revenue.value == 1250000
    assert revenue.source == FactSource.HTML_TABLE_FALLBACK

    net_income = next(f for f in facts if f.concept == FinancialConcept.NET_INCOME)
    assert net_income.value == 150000


def test_extracts_gross_profit():
    facts = _extract()
    gross_profit = next(f for f in facts if f.concept == FinancialConcept.GROSS_PROFIT)
    assert gross_profit.value == 524000


def test_unmatched_row_label_is_skipped_not_guessed():
    # "Interest expense" has no entry in LABEL_PATTERNS -- confirm it's left
    # out entirely rather than guessed into some other concept, while every
    # OTHER row (including the one added for the debt-gap-fill test) does match.
    facts = _extract()
    matched_concepts = {f.concept for f in facts}
    assert len(facts) == 4  # revenue, gross profit, net income, total debt -- not interest expense
    assert FinancialConcept.TOTAL_DEBT in matched_concepts
    assert not any(f.source_tag and "Interest expense" in f.source_tag for f in facts)


def test_every_fact_tagged_html_table_fallback():
    facts = _extract()
    assert all(f.source == FactSource.HTML_TABLE_FALLBACK for f in facts)
    assert all(f.source_tag and f.source_tag.startswith("html_table:") for f in facts)

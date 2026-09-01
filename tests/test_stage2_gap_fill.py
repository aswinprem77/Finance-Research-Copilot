import json
from datetime import date
from pathlib import Path

from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import parse_company_facts
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.schema.financial_schema import FactSource, FinancialConcept, FiscalPeriod

XBRL_FIXTURE = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"

TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def _financials_with_gap():
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, missing = parse_company_facts(raw, cik="0000320193", target_concepts=TARGET_CONCEPTS)
    coverage = compute_coverage("0000320193", TARGET_CONCEPTS, missing)
    return financials, coverage


def test_xbrl_alone_is_missing_total_debt():
    # Sanity check the premise: TOTAL_DEBT has no XBRL tag in this fixture at all.
    _, coverage = _financials_with_gap()
    assert FinancialConcept.TOTAL_DEBT in coverage.missing_concepts
    assert FinancialConcept.REVENUE not in coverage.missing_concepts


def test_html_fallback_fills_the_xbrl_gap():
    financials, coverage = _financials_with_gap()
    result = fill_coverage_gaps_from_html(
        financials, coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    assert result.filled_from_html == [FinancialConcept.TOTAL_DEBT]
    assert result.still_missing == []

    debt_facts = [f for f in result.financials.facts if f.concept == FinancialConcept.TOTAL_DEBT]
    assert len(debt_facts) == 1
    assert debt_facts[0].value == 2100000
    assert debt_facts[0].source == FactSource.HTML_TABLE_FALLBACK


def test_xbrl_resolved_concepts_are_never_touched_by_fallback():
    financials, coverage = _financials_with_gap()
    revenue_facts_before = list(financials.facts_for(FinancialConcept.REVENUE))

    result = fill_coverage_gaps_from_html(
        financials, coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    revenue_facts_after = result.financials.facts_for(FinancialConcept.REVENUE)
    # Same facts, untouched -- HTML never overrides an XBRL-resolved concept,
    # even though the HTML table also has a Total revenue row.
    assert len(revenue_facts_after) == len(revenue_facts_before)
    assert all(f.source == FactSource.XBRL for f in revenue_facts_after)


def test_no_gaps_means_html_is_never_parsed():
    financials, _ = _financials_with_gap()
    full_coverage = compute_coverage("0000320193", [FinancialConcept.REVENUE], {})  # nothing missing
    result = fill_coverage_gaps_from_html(
        financials, full_coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    assert result.filled_from_html == []
    assert result.still_missing == []


def test_gap_with_no_matching_html_row_stays_missing():
    financials, _ = _financials_with_gap()
    coverage_impossible_gap = compute_coverage(
        "0000320193", [FinancialConcept.OPERATING_INCOME],
        {FinancialConcept.OPERATING_INCOME: []},
    )
    result = fill_coverage_gaps_from_html(
        financials, coverage_impossible_gap, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    # The fixture's HTML table has no operating-income row -- must stay
    # missing rather than being guessed at.
    assert result.filled_from_html == []
    assert result.still_missing == [FinancialConcept.OPERATING_INCOME]

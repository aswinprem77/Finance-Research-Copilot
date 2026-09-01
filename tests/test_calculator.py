"""
Unit tests for the Analyst/Calculator agent. Uses the synthetic fixture — no network access
needed, runs anywhere. This is the test suite to run first in a new session to confirm the
Phase 1 code still works before building on top of it.
"""

import json
from pathlib import Path

import pytest

from src.ingestion.xbrl_parser import parse_company_facts, _DERIVABLE_Q4_CONCEPTS
from src.analyst.calculator import compute_yoy, compute_qoq, compute_margin
from src.schema.financial_schema import FactSource, FinancialConcept, FiscalPeriod

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"


@pytest.fixture
def financials():
    raw = json.loads(FIXTURE_PATH.read_text())
    parsed, missing = parse_company_facts(
        raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE, FinancialConcept.NET_INCOME]
    )
    return parsed


def test_parser_resolves_expected_tags(financials):
    revenue_facts = financials.facts_for(FinancialConcept.REVENUE)
    # Q1-Q4 2023 (4) + Q1-Q3 2024 discrete (3) + FY 2024 (1) + Q4 2024 derived (1) = 9
    assert len(revenue_facts) == 9
    directly_tagged = [f for f in revenue_facts if f.source_tag == "us-gaap:Revenues"]
    derived = [f for f in revenue_facts if f.source_tag and f.source_tag.startswith("derived:")]
    assert len(directly_tagged) == 8
    assert len(derived) == 1


def test_parser_deduplicates_correctly(financials):
    # fixture has exactly one entry per (concept, fy, fp) — confirms dedup doesn't drop
    # legitimate distinct facts
    net_income_facts = financials.facts_for(FinancialConcept.NET_INCOME)
    assert len(net_income_facts) == 4


def test_yoy_revenue_growth(financials):
    results = compute_yoy(financials, FinancialConcept.REVENUE)
    q1_yoy = next(r for r in results if r.current_fiscal_year == 2024 and r.current_period == FiscalPeriod.Q1)
    assert q1_yoy.current_value == 1050000
    assert q1_yoy.comparison_value == 1000000
    assert q1_yoy.absolute_change == 50000
    assert round(q1_yoy.percent_change, 2) == 5.0


def test_yoy_net_income_decline(financials):
    # deliberately included a YoY decline in the fixture to make sure negative changes compute
    # correctly, not just growth
    results = compute_yoy(financials, FinancialConcept.NET_INCOME)
    q1_yoy = next(r for r in results if r.current_fiscal_year == 2024 and r.current_period == FiscalPeriod.Q1)
    assert q1_yoy.current_value == 80000
    assert q1_yoy.comparison_value == 100000
    assert q1_yoy.absolute_change == -20000
    assert round(q1_yoy.percent_change, 2) == -20.0


def test_qoq_revenue(financials):
    results = compute_qoq(financials, FinancialConcept.REVENUE)
    q2_2023_qoq = next(r for r in results if r.current_fiscal_year == 2023 and r.current_period == FiscalPeriod.Q2)
    assert q2_2023_qoq.comparison_period == FiscalPeriod.Q1
    assert q2_2023_qoq.absolute_change == 100000


def test_qoq_handles_fiscal_year_rollover(financials):
    # Q1 2024 should compare against Q4 2023, crossing the fiscal year boundary
    results = compute_qoq(financials, FinancialConcept.REVENUE)
    q1_2024_qoq = next(r for r in results if r.current_fiscal_year == 2024 and r.current_period == FiscalPeriod.Q1)
    assert q1_2024_qoq.comparison_fiscal_year == 2023
    assert q1_2024_qoq.comparison_period == FiscalPeriod.Q4
    assert q1_2024_qoq.comparison_value == 1300000


def test_margin_computation(financials):
    margins = compute_margin(financials, FinancialConcept.NET_INCOME, FinancialConcept.REVENUE)
    q1_2023_margin = margins[(2023, FiscalPeriod.Q1)]
    assert round(q1_2023_margin, 2) == 10.0  # 100000 / 1000000 * 100


def test_parser_filters_ytd_cumulative_duplicates():
    # The fixture's Revenue tag now includes both a discrete Apr-Jun 2024 entry (1,250,000)
    # AND a Jan-Jun 2024 YTD-cumulative entry (2,300,000), both stamped fy=2024/fp=Q2 -- this
    # is exactly what real 10-Qs do. The parser must keep only the discrete quarter value.
    raw = json.loads(FIXTURE_PATH.read_text())
    parsed, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    q2_2024_matches = [
        f for f in parsed.facts_for(FinancialConcept.REVENUE)
        if f.fiscal_year == 2024 and f.fiscal_period == FiscalPeriod.Q2
    ]
    assert len(q2_2024_matches) == 1
    assert q2_2024_matches[0].value == 1250000  # not 2,300,000


def test_missing_concept_tracked():
    raw = json.loads(FIXTURE_PATH.read_text())
    _, missing = parse_company_facts(
        raw, cik="0000320193", target_concepts=[FinancialConcept.TOTAL_DEBT]
    )
    # fixture has no debt tags at all -- confirms the coverage-gap tracking works
    assert FinancialConcept.TOTAL_DEBT in missing


def test_q4_derived_when_fy_and_all_three_quarters_present():
    # FY2024 has FY total (5,390,000) + Q1 (1,050,000) + Q2 discrete (1,250,000, not the
    # 2,300,000 YTD dupe) + Q3 (1,300,000), but no discrete Q4 -- must derive it.
    raw = json.loads(FIXTURE_PATH.read_text())
    financials, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    q4_2024 = next(
        f for f in financials.facts_for(FinancialConcept.REVENUE)
        if f.fiscal_year == 2024 and f.fiscal_period == FiscalPeriod.Q4
    )
    assert q4_2024.value == 5390000 - 1050000 - 1250000 - 1300000  # == 1,790,000
    assert q4_2024.source == FactSource.XBRL  # still deterministic, just derived
    assert q4_2024.source_tag.startswith("derived:FY-Q1-Q2-Q3")


def test_q4_not_overridden_when_already_directly_reported():
    # FY2023 has a genuine discrete Q4 entry (from the 10-K) -- must NOT be replaced by a
    # derived value (FY2023 has no separate FY-total entry to derive from anyway).
    raw = json.loads(FIXTURE_PATH.read_text())
    financials, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    q4_2023 = next(
        f for f in financials.facts_for(FinancialConcept.REVENUE)
        if f.fiscal_year == 2023 and f.fiscal_period == FiscalPeriod.Q4
    )
    assert q4_2023.value == 1300000  # the directly-reported value, untouched
    assert q4_2023.source_tag == "us-gaap:Revenues"  # not a "derived:" tag


def test_q4_not_derived_with_partial_data():
    # NetIncomeLoss only has Q1/Q2 for both years -- no FY total, no Q3 -- must NOT derive a
    # Q4 from incomplete inputs.
    raw = json.loads(FIXTURE_PATH.read_text())
    financials, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.NET_INCOME])
    q4_matches = [
        f for f in financials.facts_for(FinancialConcept.NET_INCOME)
        if f.fiscal_period == FiscalPeriod.Q4
    ]
    assert q4_matches == []


def test_qoq_and_yoy_pick_up_derived_q4_with_no_extra_wiring():
    # compute_qoq/compute_yoy just read facts_for(concept) -- confirms the derived Q4 fact
    # flows through with zero changes needed in calculator.py.
    raw = json.loads(FIXTURE_PATH.read_text())
    financials, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])

    qoq_results = compute_qoq(financials, FinancialConcept.REVENUE)
    q4_vs_q3 = next(r for r in qoq_results if r.current_fiscal_year == 2024 and r.current_period == FiscalPeriod.Q4)
    assert q4_vs_q3.current_value == 1790000
    assert q4_vs_q3.comparison_period == FiscalPeriod.Q3

    yoy_results = compute_yoy(financials, FinancialConcept.REVENUE)
    q4_yoy = next(r for r in yoy_results if r.current_fiscal_year == 2024 and r.current_period == FiscalPeriod.Q4)
    assert q4_yoy.comparison_value == 1300000  # FY2023's directly-reported Q4


def test_total_debt_never_gets_derived_q4():
    # TOTAL_DEBT is a balance-sheet/instant concept, excluded from _DERIVABLE_Q4_CONCEPTS.
    assert FinancialConcept.TOTAL_DEBT not in _DERIVABLE_Q4_CONCEPTS

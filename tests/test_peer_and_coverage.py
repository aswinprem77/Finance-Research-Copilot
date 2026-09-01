import json
from pathlib import Path

from src.analyst.calculator import compute_peer_comparison
from src.ingestion.coverage import compute_coverage, watchlist_coverage_rate
from src.ingestion.xbrl_parser import parse_company_facts
from src.schema.financial_schema import FinancialConcept, FiscalPeriod

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"


def _load_raw():
    return json.loads(FIXTURE_PATH.read_text())


# --- peer comparison ---

def test_peer_comparison_across_two_companies():
    raw = _load_raw()
    company_a, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    company_b, _ = parse_company_facts(raw, cik="0001111111", target_concepts=[FinancialConcept.REVENUE])
    company_a.company_name = "Synthetic Test Co"
    company_b.company_name = "Peer Co B (SYNTHETIC)"

    results = compute_peer_comparison(
        [company_a, company_b], FinancialConcept.REVENUE, 2024, FiscalPeriod.Q1
    )
    assert results == [("Synthetic Test Co", 1050000), ("Peer Co B (SYNTHETIC)", 1050000)]


def test_peer_comparison_preserves_input_order_not_sorted():
    raw = _load_raw()
    company_a, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    company_b, _ = parse_company_facts(raw, cik="0001111111", target_concepts=[FinancialConcept.REVENUE])
    company_a.company_name = "Zzz Co"
    company_b.company_name = "Aaa Co"

    results = compute_peer_comparison(
        [company_a, company_b], FinancialConcept.REVENUE, 2024, FiscalPeriod.Q1
    )
    # Input order was [company_a ("Zzz Co"), company_b ("Aaa Co")] -- output should match,
    # not alphabetize or rank by value.
    assert [label for label, _ in results] == ["Zzz Co", "Aaa Co"]


def test_peer_comparison_missing_concept_returns_none():
    raw = _load_raw()
    company_a, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.TOTAL_DEBT])
    company_a.company_name = "Synthetic Test Co"
    results = compute_peer_comparison([company_a], FinancialConcept.TOTAL_DEBT, 2024, FiscalPeriod.FY)
    assert results == [("Synthetic Test Co", None)]


# --- coverage rate ---

def test_coverage_full_when_all_concepts_resolve():
    raw = _load_raw()
    concepts = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME]
    _, missing = parse_company_facts(raw, cik="0000320193", target_concepts=concepts)
    report = compute_coverage("0000320193", concepts, missing)
    assert report.coverage_rate_pct == 100.0
    assert report.missing_concepts == []


def test_coverage_partial_when_some_concepts_missing():
    raw = _load_raw()
    concepts = [FinancialConcept.REVENUE, FinancialConcept.TOTAL_DEBT]
    _, missing = parse_company_facts(raw, cik="0000320193", target_concepts=concepts)
    report = compute_coverage("0000320193", concepts, missing)
    # fixture has no debt tags at all -- exactly 1 of 2 target concepts resolves
    assert report.coverage_rate_pct == 50.0
    assert FinancialConcept.TOTAL_DEBT in report.missing_concepts
    assert FinancialConcept.REVENUE not in report.missing_concepts


def test_watchlist_coverage_rate_aggregates_across_companies():
    raw = _load_raw()
    concepts = [FinancialConcept.REVENUE, FinancialConcept.TOTAL_DEBT]

    _, missing_a = parse_company_facts(raw, cik="0000320193", target_concepts=concepts)
    report_a = compute_coverage("0000320193", concepts, missing_a)  # 1/2 resolved

    _, missing_b = parse_company_facts(raw, cik="0001111111", target_concepts=[FinancialConcept.REVENUE])
    report_b = compute_coverage("0001111111", [FinancialConcept.REVENUE], missing_b)  # 1/1 resolved

    # Weighted: (1 + 1) resolved / (2 + 1) total = 66.67%, not a plain average of the two rates
    rate = watchlist_coverage_rate([report_a, report_b])
    assert round(rate, 2) == round(2 / 3 * 100, 2)

import json
from pathlib import Path

from src.analyst.calculator import compute_qoq, compute_yoy
from src.ingestion.xbrl_parser import parse_company_facts
from src.judgment.rubric import (
    Flag,
    detect_restatements,
    flag_litigation_language,
    flag_margin_changes,
    flag_metric_changes,
)
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.schema.financial_schema import FinancialConcept

XBRL_FIXTURE = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"


def _financials():
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, _ = parse_company_facts(raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE])
    return financials


# --- flag_metric_changes ---

def test_metric_change_flags_derived_q4_yoy_spike():
    # Q4 2024 (derived) is +37.69% YoY -- clears the default 20% threshold; Q1/Q2/Q3 (+5%,
    # +13.64%, +13.04%) don't.
    results = compute_yoy(_financials(), FinancialConcept.REVENUE)
    flags = flag_metric_changes(results)
    assert len(flags) == 1
    assert flags[0].rule_id == "METRIC_CHANGE_THRESHOLD"
    assert "Q4" in flags[0].citation
    assert flags[0].severity == "notable"


def test_metric_change_respects_custom_threshold():
    results = compute_yoy(_financials(), FinancialConcept.REVENUE)
    flags_strict = flag_metric_changes(results, threshold_pct=10.0)
    # Q2 (+13.64%), Q3 (+13.04%), Q4 (+37.69%) all clear a 10% bar -- Q1 (+5%) doesn't.
    assert len(flags_strict) == 3


def test_metric_change_flags_qoq_too():
    results = compute_qoq(_financials(), FinancialConcept.REVENUE)
    flags = flag_metric_changes(results, threshold_pct=15.0)
    assert all(f.detail for f in flags)
    assert all(f.rule_id == "METRIC_CHANGE_THRESHOLD" for f in flags)


def test_every_metric_flag_has_rule_and_citation():
    results = compute_yoy(_financials(), FinancialConcept.REVENUE)
    for f in flag_metric_changes(results, threshold_pct=1.0):
        assert f.rule_id
        assert f.citation
        assert f.detail


# --- flag_margin_changes ---

def test_margin_change_flags_with_low_enough_threshold():
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, _ = parse_company_facts(
        raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE, FinancialConcept.NET_INCOME]
    )
    # Q1 net margin moved from 10.00% (FY23) to 7.62% (FY24) -- a real -2.38pp move.
    flags = flag_margin_changes(
        financials, FinancialConcept.NET_INCOME, FinancialConcept.REVENUE, threshold_pp=1.0
    )
    assert any("Q1" in f.citation for f in flags)
    assert all(f.rule_id == "MARGIN_THRESHOLD" for f in flags)


def test_margin_change_silent_below_threshold():
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, _ = parse_company_facts(
        raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE, FinancialConcept.NET_INCOME]
    )
    # Default threshold is 5pp -- neither Q1 (-2.38pp) nor Q2 (+1.09pp) clears it.
    flags = flag_margin_changes(financials, FinancialConcept.NET_INCOME, FinancialConcept.REVENUE)
    assert flags == []


# --- detect_restatements ---

def test_detects_restatement_when_same_period_has_different_values():
    raw = {
        "cik": 1,
        "entityName": "Restatement Test Co",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"start": "2023-01-01", "end": "2023-03-31", "val": 1000000,
                             "fy": 2023, "fp": "Q1", "accn": "0000000001-23-000001"},
                            # Same period, reported again in a LATER filing with a DIFFERENT value:
                            {"start": "2023-01-01", "end": "2023-03-31", "val": 950000,
                             "fy": 2023, "fp": "Q1", "accn": "0000000001-23-000050"},
                        ]
                    }
                }
            }
        },
    }
    flags = detect_restatements(raw, [FinancialConcept.REVENUE])
    assert len(flags) == 1
    assert flags[0].rule_id == "POSSIBLE_RESTATEMENT"
    assert "1000000" in flags[0].detail or "1000000.0" in flags[0].detail


def test_no_restatement_flag_when_values_agree():
    raw = {
        "cik": 1,
        "entityName": "Consistent Co",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"start": "2023-01-01", "end": "2023-03-31", "val": 1000000,
                             "fy": 2023, "fp": "Q1", "accn": "0000000001-23-000001"},
                            # Same value repeated as a comparative in a later filing -- normal, not a restatement.
                            {"start": "2023-01-01", "end": "2023-03-31", "val": 1000000,
                             "fy": 2023, "fp": "Q1", "accn": "0000000001-23-000050"},
                        ]
                    }
                }
            }
        },
    }
    flags = detect_restatements(raw, [FinancialConcept.REVENUE])
    assert flags == []


def test_restatement_check_skips_unresolvable_concepts():
    raw = {"cik": 1, "entityName": "Empty Co", "facts": {"us-gaap": {}}}
    flags = detect_restatements(raw, [FinancialConcept.REVENUE, FinancialConcept.TOTAL_DEBT])
    assert flags == []


# --- flag_litigation_language ---

def test_litigation_flag_on_real_fixture_risk_factors_chunk():
    blocks = parse_filing_html(HTML_FIXTURE.read_text())
    chunks = chunk_blocks(blocks)
    flags = flag_litigation_language(chunks)
    assert len(flags) == 1
    assert flags[0].rule_id == "LITIGATION_LANGUAGE"
    assert "Risk Factors" in flags[0].citation
    assert "complaint" in flags[0].detail.lower() or "breach of contract" in flags[0].detail.lower()


def test_litigation_flag_never_fires_on_table_chunks():
    blocks = parse_filing_html(HTML_FIXTURE.read_text())
    chunks = chunk_blocks(blocks)
    table_chunks = [c for c in chunks if c.kind == "table"]
    assert table_chunks  # sanity: fixture does have a table
    flags = flag_litigation_language(table_chunks)
    assert flags == []  # never scans tables, even if a table cell happened to contain a keyword


def test_litigation_flag_silent_with_no_matching_keywords():
    blocks = parse_filing_html(HTML_FIXTURE.read_text())
    chunks = chunk_blocks(blocks)
    flags = flag_litigation_language(chunks, keywords=["antitrust", "cybersecurity incident"])
    assert flags == []  # fixture's language doesn't match these specific (unused-here) keywords

from datetime import date
import json
from pathlib import Path

import pytest

from src.analyst.calculator import compute_yoy
from src.ingestion.xbrl_parser import parse_company_facts
from src.judgment.rubric import detect_restatements
from src.output.memo import generate_memo
from src.retrieval.html_ingest import TableBlock
from src.retrieval.html_ingest import ProseBlock, parse_filing_html
from src.retrieval.table_fallback import extract_facts_from_table
from src.schema.financial_schema import FactSource, FinancialConcept as C, FiscalPeriod as P

FIXTURES = Path(__file__).parent / "fixtures"


def entry(**updates):
    return {"start": "2024-01-01", "end": "2024-03-31", "fy": 2024, "fp": "Q1",
            "val": 100, "accn": "0000000001-24-000001", "form": "10-Q", "filed": "2024-04-20", **updates}


def raw_for(entries, tag="Revenues"):
    return {"facts": {"us-gaap": {tag: {"units": {"USD": entries}}}}}


def test_non_calendar_quarter_and_comparative_fiscal_year():
    raw = raw_for([
        entry(start="2023-10-01", end="2023-12-31", fy=2024, filed="2024-02-01", val=120),
        entry(start="2022-10-01", end="2022-12-31", fy=2024, filed="2024-02-01", val=100),
    ])
    parsed, missing = parse_company_facts(raw, "1", [C.REVENUE])
    assert not missing
    results = compute_yoy(parsed, C.REVENUE)
    assert len(results) == 1
    assert results[0].current_period == P.Q1
    assert results[0].current_fiscal_year == 2024
    assert results[0].comparison_fiscal_year == 2023
    assert results[0].percent_change == 20


def test_tag_priority_is_per_period_and_invalid_primary_can_fall_back():
    raw = raw_for([entry()])
    raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
        "units": {"USD": [entry(start="2024-04-01", end="2024-06-30", fp="Q2", accn="next", filed="2024-07-20")]}}
    parsed, missing = parse_company_facts(raw, "1", [C.REVENUE])
    assert {f.fiscal_period for f in parsed.facts} == {P.Q1, P.Q2}
    assert not missing


@pytest.mark.parametrize("entries", [[], [entry(val="NaN")], [entry(form="8-K")], [entry(start="2024-01-01", end="2024-06-30", fp="Q2")]])
def test_tag_without_usable_facts_is_a_coverage_gap(entries):
    _, missing = parse_company_facts(raw_for(entries), "1", [C.REVENUE])
    assert C.REVENUE in missing


def test_as_of_snapshot_uses_latest_available_revision_only():
    raw = raw_for([entry(), entry(val=90, accn="later", filed="2024-05-20")])
    latest, _ = parse_company_facts(raw, "1", [C.REVENUE])
    original, _ = parse_company_facts(raw, "1", [C.REVENUE], as_of=date(2024, 4, 20))
    assert latest.facts[0].value == 90
    assert original.facts[0].value == 100


def test_long_term_debt_is_not_total_debt():
    parsed, missing = parse_company_facts(raw_for([entry()], "LongTermDebt"), "1", [C.TOTAL_DEBT])
    assert not parsed.facts
    assert C.TOTAL_DEBT in missing


def test_restatement_compares_dates_not_filing_labels_or_ytd():
    raw = raw_for([entry(), entry(start="2024-01-01", end="2024-06-30", fp="Q1", val=200, accn="later")])
    assert detect_restatements(raw, [C.REVENUE]) == []
    raw = raw_for([entry(), entry(val=90, fy=2025, fp="Q2", accn="later")])
    assert len(detect_restatements(raw, [C.REVENUE])) == 1


def extract(rows, caption="in thousands"):
    return extract_facts_from_table(TableBlock("Results", caption, rows, 4), "1", 2024, P.Q2, date(2024, 6, 30))


def test_table_selects_dated_column_instead_of_first_number_and_applies_scale():
    facts = extract([["Metric", "Three Months Ended June 30, 2023", "Three Months Ended June 30, 2024"], ["Revenue", "999", "1250"]])
    assert facts[0].value == 1250000
    assert "table=4" in facts[0].source_tag


def test_table_does_not_substitute_prior_value_for_missing_current_cell():
    assert extract([["Metric", "Three Months Ended June 30, 2024", "Three Months Ended June 30, 2023"], ["Revenue", "—", "1250"]]) == []


@pytest.mark.parametrize("heading", ["2024", "Six Months Ended June 30, 2024", "Three Months Ended June 30, 2023"])
def test_table_rejects_ambiguous_or_wrong_period(heading):
    assert extract([["Metric", heading], ["Revenue", "1250"]]) == []


def test_memo_shows_uncompared_fallback_fact_and_prior_source():
    raw = json.loads((FIXTURES / "sample_companyfacts.json").read_text())
    financials, _ = parse_company_facts(raw, "320193", [C.REVENUE])
    prior = next(f for f in financials.facts if f.fiscal_year == 2023 and f.fiscal_period == P.Q1)
    prior.source = FactSource.HTML_TABLE_FALLBACK
    debt = prior.model_copy(update={"concept": C.TOTAL_DEBT, "fiscal_year": 2024, "value": 123})
    financials.facts.append(debt)
    comparisons = [r for r in compute_yoy(financials, C.REVENUE) if r.current_period == P.Q1]
    memo = generate_memo(financials, comparisons, [], "Test fixture", current_period=(2024, P.Q1))
    assert any(r.concept == "total_debt" and r.percent_change is None for r in memo.metric_rows)
    revenue = next(r for r in memo.metric_rows if r.concept == "revenue")
    assert revenue.comparison_provenance == "html_table_fallback"
    assert "sec.gov/Archives" in revenue.citation
    assert "HTML fallback" in memo.to_markdown()


def test_inline_xbrl_div_prose_is_visible_without_hidden_fact_duplicates():
    html = '''<?xml version="1.0"?><html><body>
    <ix:header><ix:hidden><div>Hidden lawsuit facts</div></ix:hidden></ix:header>
    <div><div>Item 1A. Risk Factors</div><div>A <span>new litigation</span> matter.</div></div>
    </body></html>'''
    blocks = parse_filing_html(html)
    prose = [b for b in blocks if isinstance(b, ProseBlock)]
    assert len(prose) == 1
    assert prose[0].text == "A new litigation matter."
    assert prose[0].section == "Item 1A. Risk Factors"


def test_complex_table_is_retrievable_but_not_guessed_into_facts():
    blocks = parse_filing_html('<table><tr><th colspan="2">Three Months Ended June 30, 2024</th></tr><tr><td>Revenue</td><td>100</td></tr></table>')
    assert blocks[0].complex_layout
    assert extract_facts_from_table(blocks[0], "1", 2024, P.Q2, date(2024, 6, 30)) == []

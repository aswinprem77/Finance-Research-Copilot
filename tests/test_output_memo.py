import json
from datetime import date
from pathlib import Path

import pytest

from src.analyst.calculator import compute_yoy
from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import parse_company_facts
from src.judgment.rubric import flag_metric_changes
from src.output.memo import SCOPE_DISCLAIMER, build_metric_rows, generate_memo
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.schema.financial_schema import FinancialConcept, FiscalPeriod

XBRL_FIXTURE = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent / "fixtures" / "sample_filing_excerpt.html"

TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def _financials_with_gap_filled():
    """Real end-to-end setup reusing every earlier phase: XBRL parse -> coverage
    check -> HTML fallback fill -> ready for Calculator/Judgment/Output."""
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, missing = parse_company_facts(raw, cik="0000320193", target_concepts=TARGET_CONCEPTS)
    coverage = compute_coverage("0000320193", TARGET_CONCEPTS, missing)
    result = fill_coverage_gaps_from_html(
        financials, coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    return result.financials


def test_generate_memo_requires_data_provenance_note():
    financials = _financials_with_gap_filled()
    with pytest.raises(TypeError):
        generate_memo(financials, [], [])  # missing the required data_provenance_note arg


def test_metric_rows_correctly_label_xbrl_vs_derived_vs_fallback():
    financials = _financials_with_gap_filled()
    yoy = compute_yoy(financials, FinancialConcept.REVENUE)
    rows = build_metric_rows(financials, yoy)

    xbrl_rows = [r for r in rows if r.provenance == "xbrl"]
    derived_rows = [r for r in rows if r.provenance == "derived"]
    assert xbrl_rows  # Q1/Q2/Q3 2024 and everything 2023 are directly reported
    assert len(derived_rows) == 1  # Q4 2024 is derived (FY - Q1 - Q2 - Q3)
    assert derived_rows[0].period_label == "Q4 FY2024"


def test_memo_marks_html_fallback_row_for_human_review():
    financials = _financials_with_gap_filled()
    debt_fact = next(f for f in financials.facts_for(FinancialConcept.TOTAL_DEBT))
    assert debt_fact.source.value == "html_table_fallback"  # sanity check the setup

    # TOTAL_DEBT has no prior-year XBRL data in the fixture, so compute_yoy() won't
    # produce a ComparisonResult for it -- build a row list manually to test the
    # provenance-labeling path specifically, independent of Calculator's YoY logic.
    from src.analyst.calculator import ComparisonResult
    fake_result = ComparisonResult(
        concept=FinancialConcept.TOTAL_DEBT, current_period=FiscalPeriod.Q2, current_fiscal_year=2024,
        current_value=debt_fact.value, comparison_period=FiscalPeriod.Q2, comparison_fiscal_year=2023,
        comparison_value=0, absolute_change=debt_fact.value, percent_change=None, comparison_type="YoY",
    )
    rows = build_metric_rows(financials, [fake_result])
    assert rows[0].provenance == "html_table_fallback"

    memo = generate_memo(financials, [fake_result], [], data_provenance_note="test")
    markdown = memo.to_markdown()
    assert "HTML fallback — human review" in markdown


def test_memo_never_contains_recommendation_language():
    financials = _financials_with_gap_filled()
    yoy = compute_yoy(financials, FinancialConcept.REVENUE)
    flags = flag_metric_changes(yoy)
    memo = generate_memo(
        financials, yoy, flags, data_provenance_note="SYNTHETIC TEST FIXTURE — not a real company"
    )
    markdown = memo.to_markdown()
    # The disclaimer is SUPPOSED to name buy/sell/hold, to explicitly state the memo
    # doesn't recommend them -- that line containing these words is the feature working
    # correctly, not a violation. Check the CONTENT sections (everything after the
    # disclaimer) instead, where recommendation language would actually be a real problem.
    assert SCOPE_DISCLAIMER in markdown
    content_after_disclaimer = markdown.split(SCOPE_DISCLAIMER, 1)[1].lower()
    for banned_word in ("buy", "sell", " hold ", "recommend", "should invest", "price target"):
        assert banned_word not in content_after_disclaimer


def test_memo_includes_required_provenance_note_verbatim():
    financials = _financials_with_gap_filled()
    note = "SYNTHETIC TEST FIXTURE — not a real company, for pipeline demonstration only"
    memo = generate_memo(financials, [], [], data_provenance_note=note)
    assert note in memo.to_markdown()


def test_memo_with_no_flags_says_so_explicitly():
    financials = _financials_with_gap_filled()
    memo = generate_memo(financials, [], [], data_provenance_note="test")
    assert "none" in memo.to_markdown().lower()


def test_every_flag_gets_a_human_review_line():
    financials = _financials_with_gap_filled()
    yoy = compute_yoy(financials, FinancialConcept.REVENUE)
    flags = flag_metric_changes(yoy)
    assert flags  # sanity: the derived Q4 spike should produce at least one flag
    memo = generate_memo(financials, yoy, flags, data_provenance_note="test")
    markdown = memo.to_markdown()
    assert markdown.count("For human review: yes") == len(flags)

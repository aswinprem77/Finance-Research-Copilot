"""
Gap-fill demo — shows Path A (XBRL) and Path B (HTML fallback) actually
wired together: an XBRL coverage gap automatically triggers the HTML-table
fallback for exactly the missing concept, merged into one CompanyFinancials
object. This is the piece PROGRESS.md flagged as "reported but not
connected" before this step.
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import parse_company_facts
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.schema.financial_schema import FinancialConcept, FiscalPeriod

XBRL_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_filing_excerpt.html"
TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def main() -> None:
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, missing = parse_company_facts(raw, cik="0000320193", target_concepts=TARGET_CONCEPTS)
    coverage = compute_coverage("0000320193", TARGET_CONCEPTS, missing)

    print(f"-- XBRL coverage before HTML fallback --")
    print(f"  {coverage.resolved_concepts}/{coverage.total_concepts} resolved "
          f"({coverage.coverage_rate_pct:.1f}%)")
    print(f"  Missing: {[c.value for c in coverage.missing_concepts]}\n")

    result = fill_coverage_gaps_from_html(
        financials, coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )

    print("-- After HTML-table fallback --")
    print(f"  Filled from HTML: {[c.value for c in result.filled_from_html]}")
    print(f"  Still missing (no HTML match either): {[c.value for c in result.still_missing]}\n")

    print("-- All facts for Q2 2024, with provenance --")
    for concept in TARGET_CONCEPTS:
        matches = [
            f for f in result.financials.facts_for(concept)
            if f.fiscal_year == 2024 and f.fiscal_period == FiscalPeriod.Q2
        ]
        for f in matches:
            flag = "  <- HUMAN REVIEW (not XBRL)" if f.source.value == "html_table_fallback" else ""
            print(f"  {f.concept.value}: ${f.value:,.0f}  [source={f.source.value}]{flag}")


if __name__ == "__main__":
    main()

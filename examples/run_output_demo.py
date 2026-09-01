"""
Output agent demo — this is the first demo that runs the FULL pipeline
end-to-end: XBRL parse (Path A) -> coverage check -> HTML fallback fill
(Path B) -> deterministic comparisons (Calculator) -> rubric flagging
(Judgment) -> memo (Output). Every earlier phase's real, tested code is
used here, not reimplemented.

Writes the memo to examples/sample_memo_output.md so it can be read as an
actual document, not just terminal output.
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyst.calculator import compute_yoy
from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import parse_company_facts
from src.judgment.rubric import flag_litigation_language, flag_metric_changes
from src.output.memo import generate_memo
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.schema.financial_schema import FinancialConcept, FiscalPeriod

XBRL_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_filing_excerpt.html"
OUTPUT_PATH = Path(__file__).parent / "sample_memo_output.md"

TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def main() -> None:
    # Path A: XBRL
    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, missing = parse_company_facts(raw, cik="0000320193", target_concepts=TARGET_CONCEPTS)
    coverage = compute_coverage("0000320193", TARGET_CONCEPTS, missing)

    # Path B: HTML fallback fills the XBRL gap
    gap_fill = fill_coverage_gaps_from_html(
        financials, coverage, HTML_FIXTURE.read_text(),
        fiscal_year=2024, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2024, 6, 30),
    )
    financials = gap_fill.financials

    # Calculator: deterministic comparisons
    revenue_yoy = compute_yoy(financials, FinancialConcept.REVENUE)

    # Judgment: rubric-based flagging, across both structured and unstructured sources
    flags = flag_metric_changes(revenue_yoy)
    blocks = parse_filing_html(HTML_FIXTURE.read_text())
    chunks = chunk_blocks(blocks)
    flags += flag_litigation_language(chunks)

    # Output: the memo, with a mandatory, explicit provenance note
    memo = generate_memo(
        financials,
        revenue_yoy,
        flags,
        data_provenance_note=(
            "SYNTHETIC TEST FIXTURE — not a real company. This memo demonstrates the full "
            "pipeline running end-to-end; every number and flag below is real output from "
            "real code, but the underlying financial data is fabricated for testing. "
            "See PROGRESS.md before treating any of this as representative of real filings."
        ),
    )

    markdown = memo.to_markdown()
    OUTPUT_PATH.write_text(markdown)
    print(markdown)
    print(f"\n\n(Also written to {OUTPUT_PATH})")


if __name__ == "__main__":
    main()

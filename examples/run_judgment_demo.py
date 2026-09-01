"""
Judgment Agent demo — runs all four rubric rules against the synthetic
fixtures end-to-end: Calculator output -> metric/margin threshold checks,
raw XBRL -> restatement check, Path B chunks -> litigation-language check.
Every flag below carries its rule_id and citation, per PRD v2's "no
unexplained AI judgment" requirement.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analyst.calculator import compute_yoy
from src.ingestion.xbrl_parser import parse_company_facts
from src.judgment.rubric import (
    RUBRIC_VERSION,
    detect_restatements,
    flag_litigation_language,
    flag_margin_changes,
    flag_metric_changes,
)
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.schema.financial_schema import FinancialConcept

XBRL_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_companyfacts.json"
HTML_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_filing_excerpt.html"


def _print_flags(flags) -> None:
    if not flags:
        print("  (none)")
        return
    for f in flags:
        print(f"  [{f.rule_id}] {f.detail}")
        print(f"      cites: {f.citation}")


def main() -> None:
    print(f"Rubric version: {RUBRIC_VERSION}\n")

    raw = json.loads(XBRL_FIXTURE.read_text())
    financials, _ = parse_company_facts(
        raw, cik="0000320193", target_concepts=[FinancialConcept.REVENUE, FinancialConcept.NET_INCOME]
    )

    print("-- Metric change flags (YoY revenue, default 20% threshold) --")
    _print_flags(flag_metric_changes(compute_yoy(financials, FinancialConcept.REVENUE)))

    print("\n-- Margin change flags (net margin, threshold lowered to 1pp for this demo) --")
    _print_flags(
        flag_margin_changes(
            financials, FinancialConcept.NET_INCOME, FinancialConcept.REVENUE, threshold_pp=1.0
        )
    )

    print("\n-- Restatement check (revenue) --")
    _print_flags(detect_restatements(raw, [FinancialConcept.REVENUE]))
    print("  (fixture has no conflicting values -- this is the expected 'clean' case;")
    print("   see test_judgment_rubric.py for the flagged case with an inline example)")

    print("\n-- Litigation-language check (HTML fixture, Risk Factors section) --")
    blocks = parse_filing_html(HTML_FIXTURE.read_text())
    chunks = chunk_blocks(blocks)
    _print_flags(flag_litigation_language(chunks))


if __name__ == "__main__":
    main()

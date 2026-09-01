"""
Phase 1 demo — runs the XBRL ingestion + calculator pipeline end-to-end
against the synthetic fixture (this sandbox can't reach data.sec.gov
directly — see PROGRESS.md). To run against a real filer, use
SecXbrlClient.get_company_facts() instead of the fixture loader below
(see README "Try Phase 1 against a real filing").
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so `src.*` resolves when run directly

from src.analyst.calculator import compute_margin, compute_peer_comparison, compute_qoq, compute_yoy
from src.ingestion.coverage import compute_coverage, watchlist_coverage_rate
from src.ingestion.xbrl_parser import parse_company_facts
from src.schema.financial_schema import FinancialConcept, FiscalPeriod

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_companyfacts.json"
TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def main() -> None:
    raw = json.loads(FIXTURE_PATH.read_text())
    financials, missing = parse_company_facts(raw, cik="0000320193", target_concepts=TARGET_CONCEPTS)

    print(f"Company: {financials.company_name} (CIK {financials.company_cik})\n")

    print("-- YoY: Revenue --")
    for r in compute_yoy(financials, FinancialConcept.REVENUE):
        print(
            f"  {r.current_period.value} FY{r.current_fiscal_year}: ${r.current_value:,.0f}  "
            f"(vs {r.comparison_period.value} FY{r.comparison_fiscal_year}: ${r.comparison_value:,.0f}, "
            f"{r.percent_change:+.2f}%)"
        )

    print("\n-- QoQ: Revenue --")
    for r in compute_qoq(financials, FinancialConcept.REVENUE):
        print(
            f"  {r.current_period.value} FY{r.current_fiscal_year} vs "
            f"{r.comparison_period.value} FY{r.comparison_fiscal_year}: {r.percent_change:+.2f}%"
        )

    print("\n-- Net margin by period --")
    for (fy, fp), m in sorted(compute_margin(financials, FinancialConcept.NET_INCOME).items()):
        print(f"  FY{fy} {fp.value}: {m:.2f}%" if m is not None else f"  FY{fy} {fp.value}: N/A")

    print("\n-- XBRL coverage --")
    report = compute_coverage(financials.company_cik, TARGET_CONCEPTS, missing)
    print(f"  {report.resolved_concepts}/{report.total_concepts} target concepts resolved "
          f"({report.coverage_rate_pct:.1f}%)")
    print(f"  Missing: {[c.value for c in report.missing_concepts] or 'none'}")

    print("\n-- Peer comparison (Revenue, Q1 2024) --")
    # Using the same fixture twice as a stand-in second peer, since this
    # synthetic fixture only represents one real company.
    peer_financials, _ = parse_company_facts(raw, cik="0001111111", target_concepts=[FinancialConcept.REVENUE])
    peer_financials.company_name = "Peer Co B (SYNTHETIC)"
    for name, value in compute_peer_comparison(
        [financials, peer_financials], FinancialConcept.REVENUE, 2024, FiscalPeriod.Q1
    ):
        print(f"  {name}: ${value:,.0f}" if value is not None else f"  {name}: N/A")


if __name__ == "__main__":
    main()

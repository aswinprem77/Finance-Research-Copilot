"""
Watchlist demo — loads the locked-in semiconductor watchlist and runs
coverage checking across all 7 companies. Uses the synthetic fixture as a
stand-in fetch function for every company (same "real logic, synthetic
data" approach as the other demos) since this sandbox can't reach
data.sec.gov. Swap `_stand_in_fetch` for a real SecXbrlClient once run
somewhere with network access -- run_watchlist_coverage() itself needs no
changes either way.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ingestion.coverage import watchlist_coverage_rate
from src.pipeline.watchlist import load_watchlist, run_watchlist_coverage
from src.schema.financial_schema import FinancialConcept

XBRL_FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_companyfacts.json"
TARGET_CONCEPTS = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]


def _stand_in_fetch(cik: str) -> dict:
    return json.loads(XBRL_FIXTURE.read_text())


def main() -> None:
    watchlist = load_watchlist()
    print(f"Watchlist: {watchlist.sector} ({len(watchlist.companies)} companies)")
    for c in watchlist.companies:
        print(f"  {c.ticker:6s} {c.name} (CIK {c.cik})")

    print(f"\n-- Coverage per company (target: {[c.value for c in TARGET_CONCEPTS]}) --")
    print("   NOTE: every company below is using the SAME synthetic fixture as a stand-in --")
    print("   this demonstrates the orchestration loop, not real per-company coverage.\n")
    reports = run_watchlist_coverage(watchlist, TARGET_CONCEPTS, _stand_in_fetch)
    for company, report in zip(watchlist.companies, reports):
        print(f"  {company.ticker:6s}: {report.resolved_concepts}/{report.total_concepts} "
              f"({report.coverage_rate_pct:.1f}%) — missing {[c.value for c in report.missing_concepts]}")

    aggregate = watchlist_coverage_rate(reports)
    print(f"\nAggregate watchlist coverage rate: {aggregate:.1f}%  (PRD Section 4 target: >=90%)")


if __name__ == "__main__":
    main()

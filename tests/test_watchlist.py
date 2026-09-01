import json
from pathlib import Path

from src.ingestion.coverage import watchlist_coverage_rate
from src.pipeline.watchlist import load_watchlist, run_watchlist_coverage
from src.schema.financial_schema import FinancialConcept

XBRL_FIXTURE = Path(__file__).parent / "fixtures" / "sample_companyfacts.json"


def _stand_in_fetch(cik: str) -> dict:
    # Every real company gets the SAME synthetic fixture data here -- this
    # tests the ORCHESTRATION loop (does it call the fetch fn per company
    # and aggregate correctly?), not real per-company financials. See
    # PROGRESS.md: this is exactly the same "synthetic stand-in, real logic"
    # approach used everywhere else pending live network access.
    return json.loads(XBRL_FIXTURE.read_text())


def test_load_watchlist_reads_locked_in_companies():
    watchlist = load_watchlist()
    assert watchlist.sector == "Semiconductors"
    tickers = {c.ticker for c in watchlist.companies}
    assert tickers == {"NVDA", "AMD", "INTC", "AVGO", "QCOM", "MU", "TXN"}
    assert len(watchlist.companies) == 7  # PRD wants 5-10, single sector


def test_watchlist_ciks_are_nonempty_and_distinct():
    watchlist = load_watchlist()
    ciks = [c.cik for c in watchlist.companies]
    assert all(cik for cik in ciks)
    assert len(ciks) == len(set(ciks))  # no duplicate CIKs


def test_run_watchlist_coverage_produces_one_report_per_company():
    watchlist = load_watchlist()
    concepts = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]
    reports = run_watchlist_coverage(watchlist, concepts, _stand_in_fetch)
    assert len(reports) == len(watchlist.companies)
    assert {r.company_cik for r in reports} == {c.cik for c in watchlist.companies}


def test_run_watchlist_coverage_feeds_aggregate_rate():
    watchlist = load_watchlist()
    concepts = [FinancialConcept.REVENUE, FinancialConcept.NET_INCOME, FinancialConcept.TOTAL_DEBT]
    reports = run_watchlist_coverage(watchlist, concepts, _stand_in_fetch)
    rate = watchlist_coverage_rate(reports)
    # Every company gets the same fixture (2/3 concepts resolve) -> aggregate == per-company rate
    assert round(rate, 2) == round(2 / 3 * 100, 2)

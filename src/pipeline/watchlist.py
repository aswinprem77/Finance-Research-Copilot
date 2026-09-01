"""
Watchlist config loading + a thin orchestrator that runs Stage 2 coverage
checking across every company in the watchlist, per PRD v2 Section 6.

Locked-in watchlist (see config/watchlist.json): 7 large-cap semiconductor
companies, real and verified CIKs. Chosen sector rationale is in the config
file itself, not duplicated here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.ingestion.coverage import CoverageReport, compute_coverage
from src.ingestion.xbrl_parser import parse_company_facts
from src.schema.financial_schema import FinancialConcept

DEFAULT_WATCHLIST_PATH = Path(__file__).parent.parent.parent / "config" / "watchlist.json"


@dataclass
class WatchlistCompany:
    name: str
    ticker: str
    cik: str


@dataclass
class Watchlist:
    sector: str
    companies: list[WatchlistCompany]


def load_watchlist(path: Path | str = DEFAULT_WATCHLIST_PATH) -> Watchlist:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    companies = [WatchlistCompany(name=c["name"], ticker=c["ticker"], cik=c["cik"]) for c in raw["companies"]]
    return Watchlist(sector=raw["sector"], companies=companies)


def run_watchlist_coverage(
    watchlist: Watchlist,
    target_concepts: list[FinancialConcept],
    fetch_companyfacts_fn: Callable[[str], dict],
) -> list[CoverageReport]:
    """
    For each company, calls `fetch_companyfacts_fn(cik)` to get raw XBRL
    JSON and computes its coverage report. `fetch_companyfacts_fn` is
    injected rather than hardcoded to SecXbrlClient.get_company_facts so
    this loop is testable offline: production passes the real client
    method, tests pass a stand-in that returns fixture data. This is what
    actually produces the per-company reports that
    coverage.watchlist_coverage_rate() aggregates -- that function existed
    since the coverage-rate metric was added, but nothing called it across
    a real company list until now.
    """
    reports = []
    for company in watchlist.companies:
        raw = fetch_companyfacts_fn(company.cik)
        _, missing = parse_company_facts(raw, cik=company.cik, target_concepts=target_concepts)
        reports.append(compute_coverage(company.cik, target_concepts, missing))
    return reports

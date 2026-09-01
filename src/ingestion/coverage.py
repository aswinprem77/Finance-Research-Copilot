"""
XBRL coverage-rate tracking — the metric added in PRD v2 Section 4:
"XBRL coverage rate (% of required line items resolved without HTML-table
fallback) >= 90% across watchlist."

parse_company_facts() already returns a `missing` dict (concept -> tags it
tried and couldn't find). This module turns that into the actual reportable
metric, per filer, so it can be tracked across the watchlist over time.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.schema.financial_schema import FinancialConcept


@dataclass
class CoverageReport:
    company_cik: str
    total_concepts: int
    resolved_concepts: int
    missing_concepts: list[FinancialConcept]

    @property
    def coverage_rate_pct(self) -> float:
        if self.total_concepts == 0:
            return 0.0
        return self.resolved_concepts / self.total_concepts * 100.0


def compute_coverage(
    company_cik: str,
    target_concepts: list[FinancialConcept],
    missing: dict[FinancialConcept, list[str]],
) -> CoverageReport:
    """
    `target_concepts` and `missing` are exactly what you pass to / get back from
    parse_company_facts() — this function doesn't re-derive anything, just turns
    the gap list into a reportable rate.
    """
    missing_concepts = list(missing.keys())
    resolved = len(target_concepts) - len(missing_concepts)
    return CoverageReport(
        company_cik=company_cik,
        total_concepts=len(target_concepts),
        resolved_concepts=resolved,
        missing_concepts=missing_concepts,
    )


def watchlist_coverage_rate(reports: list[CoverageReport]) -> float:
    """
    Aggregate coverage rate across the whole watchlist (PRD's actual metric is
    stated "across watchlist", not per-company) — total resolved / total target,
    weighted by each company's concept count.
    """
    total_concepts = sum(r.total_concepts for r in reports)
    total_resolved = sum(r.resolved_concepts for r in reports)
    if total_concepts == 0:
        return 0.0
    return total_resolved / total_concepts * 100.0

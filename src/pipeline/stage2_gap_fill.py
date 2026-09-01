"""
Stage 2 orchestration — combines Path A (XBRL) and Path B (HTML fallback)
into the single shared schema, per PRD v2 Section 5: "Everything downstream
... consumes both into one shared schema, but they don't take the same
path in." This is the piece PROGRESS.md flagged as not yet connected: an
XBRL coverage gap should actually trigger the HTML-table fallback for the
missing concepts, not just report the gap and stop there.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.ingestion.coverage import CoverageReport
from src.retrieval.html_ingest import TableBlock, parse_filing_html
from src.retrieval.table_fallback import extract_facts_from_table
from src.schema.financial_schema import CompanyFinancials, FinancialConcept, FiscalPeriod


@dataclass
class GapFillResult:
    financials: CompanyFinancials
    filled_from_html: list[FinancialConcept]
    still_missing: list[FinancialConcept]


def fill_coverage_gaps_from_html(
    financials: CompanyFinancials,
    coverage: CoverageReport,
    filing_html: str,
    fiscal_year: int,
    fiscal_period: FiscalPeriod,
    period_end_date: date,
) -> GapFillResult:
    """
    For each concept `coverage` says XBRL couldn't resolve, parse
    `filing_html` and look for a matching HTML-table row. A match gets
    merged into `financials.facts`, tagged source=HTML_TABLE_FALLBACK by
    extract_facts_from_table() — so it's flagged for human review
    downstream (PRD v2 Output stage), never silently indistinguishable
    from an XBRL-sourced number.

    XBRL-resolved concepts are never touched here — this function only
    ever ADDS facts for concepts that were missing, so XBRL stays primary
    per PRD v2 regardless of what the HTML also contains. If a concept is
    missing from XBRL and the HTML has no matching row either, it stays
    missing — this doesn't guess, same philosophy as the rest of the
    fallback path (see table_fallback.py).

    Stops scanning further tables once every gap is filled, and never
    fills the same concept twice from two different tables (first match
    wins) — both to keep this cheap and to avoid picking up a stray
    same-labeled figure from an unrelated table further down the filing.
    """
    still_missing = set(coverage.missing_concepts)
    filled: list[FinancialConcept] = []

    if still_missing:
        blocks = parse_filing_html(filing_html)
        table_blocks = [b for b in blocks if isinstance(b, TableBlock)]

        for table in table_blocks:
            if not still_missing:
                break
            candidate_facts = extract_facts_from_table(
                table,
                company_cik=financials.company_cik,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                period_end_date=period_end_date,
            )
            for fact in candidate_facts:
                if fact.concept in still_missing:
                    financials.facts.append(fact)
                    filled.append(fact.concept)
                    still_missing.discard(fact.concept)

    return GapFillResult(
        financials=financials,
        filled_from_html=filled,
        still_missing=sorted(still_missing, key=lambda c: c.value),
    )

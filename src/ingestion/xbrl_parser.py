"""
Parses raw SEC XBRL companyfacts JSON into FinancialFact objects (Path A, PRD v2 Section 5).

Per PRD v2's risk table: US-GAAP tags are NOT perfectly consistent across filers. Two companies
reporting "the same" line item may use different tags (e.g. Revenues vs.
RevenueFromContractWithCustomerExcludingAssessedTax). TAG_FALLBACKS below tries a list of known
tags per concept, in priority order, and records which one actually resolved — this is the raw
material for the XBRL coverage-rate metric in PRD v2 Section 4, instrumented in
src/ingestion/coverage.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.schema.financial_schema import (
    CompanyFinancials,
    FactSource,
    FinancialConcept,
    FinancialFact,
    FiscalPeriod,
)

# Ordered by preference — first match wins. Expand this as you test against real filers;
# this is the exact list PROGRESS.md flags as needing growth.
TAG_FALLBACKS: dict[FinancialConcept, list[str]] = {
    FinancialConcept.REVENUE: [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    FinancialConcept.NET_INCOME: [
        "NetIncomeLoss",
        "ProfitLoss",
    ],
    FinancialConcept.GROSS_PROFIT: [
        "GrossProfit",
    ],
    FinancialConcept.TOTAL_DEBT: [
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebt",
        "LongTermDebtNoncurrent",
    ],
    FinancialConcept.OPERATING_INCOME: [
        "OperatingIncomeLoss",
    ],
}

_QUARTER_FROM_MONTHS = {3: FiscalPeriod.Q1, 6: FiscalPeriod.Q2, 9: FiscalPeriod.Q3, 12: FiscalPeriod.Q4}

# Concepts where quarterly figures are additive across a fiscal year (flow/duration items,
# e.g. revenue earned during a period) -- eligible for Q4 derivation via FY - Q1 - Q2 - Q3.
# TOTAL_DEBT is deliberately excluded: it's a balance-sheet snapshot (an instant, not a
# duration), so it has no "FY total" to subtract from -- its Q4 value IS the fiscal-year-end
# balance, which the instant-fact branch of _infer_fiscal_period already resolves directly.
# Applying subtraction to an instant concept would produce a meaningless number.
_DERIVABLE_Q4_CONCEPTS = {
    FinancialConcept.REVENUE,
    FinancialConcept.NET_INCOME,
    FinancialConcept.GROSS_PROFIT,
    FinancialConcept.OPERATING_INCOME,
}


def _infer_fiscal_period(start: Optional[str], end: str, form: str) -> Optional[FiscalPeriod]:
    """
    XBRL 'frames' don't explicitly label Q1/Q2/Q3/FY the way a human would — infer it from
    the DURATION of the period, not the form type. This matters because a 10-K's revenue tag
    almost always spans the full fiscal year (start..end ~365 days), not just Q4 — there is
    no such thing as a "Q4-only" duration fact in standard XBRL. Span-based inference also
    generalizes to instant facts (balance-sheet items like debt, which have no 'start' at all).

    FIXED (was a KNOWN GAP, see git history / PROGRESS.md): because Q4-only duration facts
    often don't exist directly in XBRL for filers who only tag the full fiscal year, true Q4
    figures for those filers must be derived as FY - Q1 - Q2 - Q3 rather than read directly.
    This function still only classifies what's DIRECTLY present (a period this long IS a
    quarter, IS a year, or IS neither) -- the derivation itself happens in
    _derive_missing_q4_facts() below, as a post-processing step in parse_company_facts(),
    since it needs the already-classified FY/Q1/Q2/Q3 facts together, not a single entry
    in isolation like this function sees.

    SECOND GAP THIS FUNCTION MUST GUARD AGAINST: real 10-Qs frequently tag the SAME concept
    twice under the SAME fy/fp label — once as a discrete ~3-month "quarter" duration, and
    once as a ~6-month or ~9-month "year-to-date" cumulative (e.g. a Q2 10-Q's revenue tag
    includes both an Apr-Jun entry AND a Jan-Jun entry, both stamped fp="Q2"). Classifying by
    end-month alone (the original version of this function) would treat whichever one the
    dedup step happened to keep as "the" quarter, silently mixing 3-month and 6-month figures.
    So a duration fact is only accepted as a genuine single quarter when its span is close to
    one quarter (75-100 days); anything between quarter-length and year-length is a YTD
    cumulative and is filtered out entirely (returns None) rather than misclassified. Deriving
    discrete quarters from YTD cumulatives (YTD_Q2 - Q1) is deferred, same as the Q4 gap above.
    """
    end_date = date.fromisoformat(end)
    if start is None:
        # Instant fact (e.g. debt balance at period end) -- no duration to measure, infer
        # quarter from the end month as a best guess.
        return _QUARTER_FROM_MONTHS.get(end_date.month, FiscalPeriod.Q4)

    start_date = date.fromisoformat(start)
    span_days = (end_date - start_date).days

    if span_days > 300:  # roughly a year
        return FiscalPeriod.FY
    if 75 <= span_days <= 100:  # roughly one quarter
        return _QUARTER_FROM_MONTHS.get(end_date.month, FiscalPeriod.Q4)
    # Between one quarter and one year: a YTD cumulative duration, not a discrete
    # quarter. Skip it here rather than let it collide with the real quarter value.
    return None


def _derive_missing_q4_facts(deduped: dict[tuple, FinancialFact]) -> list[FinancialFact]:
    """
    If a filer's FY total and all three of Q1/Q2/Q3 are resolved (directly, post-dedup) for
    a flow concept, Q4 can be derived as FY - Q1 - Q2 - Q3 -- still fully deterministic (PRD
    requirement: no LLM-guessed numbers), just arithmetic on four already-real XBRL facts,
    not a new tag lookup. Only fires when Q4 ISN'T already directly present (some filers do
    tag a discrete Q4 duration in their 10-K, e.g. as supplementary data -- never override
    a real reported figure with a derived one). Needs all four inputs present; a filer
    missing even one of FY/Q1/Q2/Q3 doesn't get a derived Q4 -- partial data isn't guessed at,
    same philosophy as everywhere else in this parser.

    Returns only the newly derived facts; doesn't mutate `deduped`.
    """
    derived: list[FinancialFact] = []
    candidate_years = {
        (concept, fy) for (concept, fy, _period) in deduped if concept in _DERIVABLE_Q4_CONCEPTS
    }

    for concept, fy in candidate_years:
        if (concept, fy, FiscalPeriod.Q4) in deduped:
            continue  # already directly reported -- don't override with a derived value

        fy_fact = deduped.get((concept, fy, FiscalPeriod.FY))
        q1 = deduped.get((concept, fy, FiscalPeriod.Q1))
        q2 = deduped.get((concept, fy, FiscalPeriod.Q2))
        q3 = deduped.get((concept, fy, FiscalPeriod.Q3))
        if not (fy_fact and q1 and q2 and q3):
            continue  # need all four to derive reliably

        derived.append(
            FinancialFact(
                company_cik=fy_fact.company_cik,
                concept=concept,
                value=fy_fact.value - q1.value - q2.value - q3.value,
                unit=fy_fact.unit,
                fiscal_year=fy,
                fiscal_period=FiscalPeriod.Q4,
                period_end_date=fy_fact.period_end_date,  # fiscal year end IS Q4's end
                filed_date=fy_fact.filed_date,
                source=FactSource.XBRL,  # still fully deterministic, just derived by subtraction
                source_tag=f"derived:FY-Q1-Q2-Q3 (FY tag: {fy_fact.source_tag})",
                accession_number=fy_fact.accession_number,
            )
        )
    return derived


def parse_company_facts(
    raw: dict,
    cik: str,
    target_concepts: Optional[list[FinancialConcept]] = None,
) -> tuple[CompanyFinancials, dict[FinancialConcept, list[str]]]:
    """
    Parse raw SEC companyfacts JSON into a CompanyFinancials object.

    Returns (parsed_financials, missing_tags) where missing_tags maps any target concept that
    had NO matching tag in this filer's data — this is the coverage-gap signal that should feed
    Path B fallback (per PRD v2) and the coverage-rate metric (PRD v2 Section 4, see coverage.py).
    """
    target_concepts = target_concepts or list(FinancialConcept)
    company_name = raw.get("entityName")
    us_gaap = raw.get("facts", {}).get("us-gaap", {})

    facts: list[FinancialFact] = []
    missing: dict[FinancialConcept, list[str]] = {}

    for concept in target_concepts:
        candidate_tags = TAG_FALLBACKS.get(concept, [])
        resolved_tag = None

        for tag in candidate_tags:
            if tag in us_gaap:
                resolved_tag = tag
                break

        if resolved_tag is None:
            missing[concept] = candidate_tags
            continue

        units = us_gaap[resolved_tag].get("units", {})
        usd_entries = units.get("USD", [])

        for entry in usd_entries:
            # Only keep entries that represent a discrete filed fact (has an accession number
            # and form type) — companyfacts includes some derived/aggregated entries we don't
            # want here.
            if "accn" not in entry or "form" not in entry:
                continue
            if entry["form"] not in ("10-K", "10-Q"):
                continue

            try:
                period = _infer_fiscal_period(entry.get("start"), entry["end"], entry["form"])
                if period is None:
                    # A YTD-cumulative duration (or otherwise unclassifiable span) —
                    # not a discrete quarter or full year. See _infer_fiscal_period.
                    continue
                facts.append(
                    FinancialFact(
                        company_cik=cik,
                        concept=concept,
                        value=float(entry["val"]),
                        unit="USD",
                        fiscal_year=entry.get("fy", date.fromisoformat(entry["end"]).year),
                        fiscal_period=period,
                        period_end_date=date.fromisoformat(entry["end"]),
                        filed_date=date.fromisoformat(entry["filed"]) if "filed" in entry else None,
                        source=FactSource.XBRL,
                        source_tag=f"us-gaap:{resolved_tag}",
                        accession_number=entry.get("accn"),
                    )
                )
            except (KeyError, ValueError):
                # Malformed entry — skip rather than crash the whole parse. Worth logging in a
                # real run; PROGRESS.md notes this needs proper logging, not just a silent skip.
                continue

    # De-duplicate: companyfacts often repeats the same fact across multiple filings
    # (e.g. a Q1 figure reported again as a comparative in the next 10-Q). Keep the earliest
    # filed version of each (concept, fiscal_year, fiscal_period) triple as canonical.
    deduped: dict[tuple, FinancialFact] = {}
    for f in facts:
        key = (f.concept, f.fiscal_year, f.fiscal_period)
        if key not in deduped or (f.filed_date and deduped[key].filed_date and f.filed_date < deduped[key].filed_date):
            deduped[key] = f

    for derived_fact in _derive_missing_q4_facts(deduped):
        key = (derived_fact.concept, derived_fact.fiscal_year, derived_fact.fiscal_period)
        deduped[key] = derived_fact

    result = CompanyFinancials(
        company_cik=cik,
        company_name=company_name,
        facts=list(deduped.values()),
    )
    return result, missing

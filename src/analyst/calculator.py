"""
Analyst Agent — deterministic financial calculator (PRD Section 5, Stage 3).

Per PRD: "Computes deterministic comparisons in Python (not LLM arithmetic): YoY, QoQ, vs.
peer set." This module is pure Python / pure math — no LLM call anywhere in this file, by design.
That's the whole point: this is the part of the pipeline that must be 100% numerically correct,
not "usually right."

Covers single-company YoY/QoQ/margin and peer-vs-peer comparison (compute_peer_comparison).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.schema.financial_schema import CompanyFinancials, FinancialConcept, FinancialFact, FiscalPeriod


@dataclass
class ComparisonResult:
    concept: FinancialConcept
    current_period: FiscalPeriod
    current_fiscal_year: int
    current_value: float
    comparison_period: FiscalPeriod
    comparison_fiscal_year: int
    comparison_value: float
    absolute_change: float
    percent_change: Optional[float]  # None if comparison_value == 0 (avoid div-by-zero)
    comparison_type: str  # "YoY" or "QoQ"


def _pct_change(current: float, prior: float) -> Optional[float]:
    if prior == 0:
        return None
    return (current - prior) / abs(prior) * 100.0


def compute_yoy(financials: CompanyFinancials, concept: FinancialConcept) -> list[ComparisonResult]:
    """
    Year-over-year comparison: for each fact in a given period type (e.g. Q2 2026), find the
    same period one fiscal year earlier (Q2 2025) and compute change. Works for both FY (10-K)
    and quarterly (10-Q) facts — comparison is always same-period-type, prior year.
    """
    facts = sorted(financials.facts_for(concept), key=lambda f: (f.fiscal_year, f.fiscal_period.value))
    by_key = {(f.fiscal_year, f.fiscal_period): f for f in facts}

    results = []
    for f in facts:
        prior_key = (f.fiscal_year - 1, f.fiscal_period)
        prior = by_key.get(prior_key)
        if prior is None:
            continue
        results.append(
            ComparisonResult(
                concept=concept,
                current_period=f.fiscal_period,
                current_fiscal_year=f.fiscal_year,
                current_value=f.value,
                comparison_period=prior.fiscal_period,
                comparison_fiscal_year=prior.fiscal_year,
                comparison_value=prior.value,
                absolute_change=f.value - prior.value,
                percent_change=_pct_change(f.value, prior.value),
                comparison_type="YoY",
            )
        )
    return results


_QUARTER_ORDER = [FiscalPeriod.Q1, FiscalPeriod.Q2, FiscalPeriod.Q3, FiscalPeriod.Q4]


def compute_qoq(financials: CompanyFinancials, concept: FinancialConcept) -> list[ComparisonResult]:
    """
    Quarter-over-quarter comparison. Only meaningful for quarterly facts (Q1-Q4) — FY facts are
    skipped. Handles fiscal-year rollover (Q1 20XX compares to Q4 20XX-1).
    """
    facts = [f for f in financials.facts_for(concept) if f.fiscal_period != FiscalPeriod.FY]
    by_key = {(f.fiscal_year, f.fiscal_period): f for f in facts}

    results = []
    for f in facts:
        idx = _QUARTER_ORDER.index(f.fiscal_period)
        if idx == 0:
            prior_key = (f.fiscal_year - 1, FiscalPeriod.Q4)
        else:
            prior_key = (f.fiscal_year, _QUARTER_ORDER[idx - 1])

        prior = by_key.get(prior_key)
        if prior is None:
            continue

        results.append(
            ComparisonResult(
                concept=concept,
                current_period=f.fiscal_period,
                current_fiscal_year=f.fiscal_year,
                current_value=f.value,
                comparison_period=prior.fiscal_period,
                comparison_fiscal_year=prior.fiscal_year,
                comparison_value=prior.value,
                absolute_change=f.value - prior.value,
                percent_change=_pct_change(f.value, prior.value),
                comparison_type="QoQ",
            )
        )
    return results


def compute_margin(
    financials: CompanyFinancials,
    numerator_concept: FinancialConcept,
    denominator_concept: FinancialConcept = FinancialConcept.REVENUE,
) -> dict[tuple[int, FiscalPeriod], Optional[float]]:
    """
    E.g. compute_margin(financials, NET_INCOME, REVENUE) -> net margin per period.
    Returns None for a period where revenue is 0 or missing (avoid div-by-zero / bad match).
    """
    numerator_facts = {(f.fiscal_year, f.fiscal_period): f.value for f in financials.facts_for(numerator_concept)}
    denominator_facts = {(f.fiscal_year, f.fiscal_period): f.value for f in financials.facts_for(denominator_concept)}

    margins: dict[tuple[int, FiscalPeriod], Optional[float]] = {}
    for key, num in numerator_facts.items():
        denom = denominator_facts.get(key)
        margins[key] = (num / denom * 100.0) if denom else None
    return margins


def compute_peer_comparison(
    peer_financials: list[CompanyFinancials],
    concept: FinancialConcept,
    fiscal_year: int,
    fiscal_period: FiscalPeriod = FiscalPeriod.FY,
) -> list[tuple[str, Optional[float]]]:
    """
    Same-period figure across a peer set (PRD Section 5, Stage 3: "vs. peer set").

    Returns (company label, value or None) pairs in the SAME order as peer_financials —
    this function doesn't rank or sort; the caller decides display/ranking order. A peer
    missing the concept for that period gets None rather than being silently dropped, so
    the caller can render "N/A" instead of an incomplete-looking peer set.
    """
    results: list[tuple[str, Optional[float]]] = []
    for cf in peer_financials:
        match = next(
            (
                f for f in cf.facts_for(concept)
                if f.fiscal_year == fiscal_year and f.fiscal_period == fiscal_period
            ),
            None,
        )
        label = cf.company_name or cf.company_ticker or cf.company_cik
        results.append((label, match.value if match else None))
    return results

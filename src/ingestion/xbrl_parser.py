"""Normalize companyfacts by reporting dates, with optional as-of snapshots.

SEC fy/fp describe the filing context, including comparative observations.
"""
from __future__ import annotations

import logging
import math
from datetime import date

from src.schema.financial_schema import CompanyFinancials, FactSource, FinancialConcept, FinancialFact, FiscalPeriod

logger = logging.getLogger(__name__)
TAG_FALLBACKS = {
    FinancialConcept.REVENUE: ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    FinancialConcept.NET_INCOME: ["NetIncomeLoss", "ProfitLoss"],
    FinancialConcept.GROSS_PROFIT: ["GrossProfit"],
    # LongTermDebt alone is not total debt: leave that coverage gap explicit.
    FinancialConcept.TOTAL_DEBT: ["DebtLongtermAndShorttermCombinedAmount"],
    FinancialConcept.OPERATING_INCOME: ["OperatingIncomeLoss"],
}
_DERIVABLE_Q4_CONCEPTS = set(FinancialConcept) - {FinancialConcept.TOTAL_DEBT}


def snapshot_company_facts(raw: dict, as_of: date | None = None) -> dict:
    """Exclude observations filed after the event being analyzed (no look-ahead)."""
    if as_of is None:
        return raw
    facts = {}
    for namespace, tags in raw.get("facts", {}).items():
        facts[namespace] = {}
        for tag, payload in tags.items():
            units = {}
            for unit, entries in payload.get("units", {}).items():
                valid = []
                for entry in entries:
                    try:
                        if date.fromisoformat(entry["filed"]) <= as_of:
                            valid.append(entry)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Skipping observation without a valid filed date: %s", tag)
                units[unit] = valid
            facts[namespace][tag] = {**payload, "units": units}
    return {**raw, "facts": facts}


def _contexts(us_gaap: dict) -> dict:
    contexts = {}
    for payload in us_gaap.values():
        for entries in payload.get("units", {}).values():
            for entry in entries:
                try:
                    if entry.get("form") not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
                        continue
                    end = date.fromisoformat(entry["end"])
                    fy = int(entry["fy"])
                    fp = FiscalPeriod(entry["fp"])
                    accession = entry["accn"]
                    if accession not in contexts or end > contexts[accession][0]:
                        contexts[accession] = (end, fy, fp)
                except (KeyError, TypeError, ValueError):
                    continue
    return contexts


def _period(entry: dict, context: tuple) -> tuple[int, FiscalPeriod]:
    end = date.fromisoformat(entry["end"])
    report_end, report_fy, report_fp = context
    quarter = 4 if report_fp == FiscalPeriod.FY else int(report_fp.value[1])
    days_back = (report_end - end).days
    quarters_back = round(days_back / 91.3125)
    if days_back < 0 or abs(days_back - quarters_back * 91.3125) > 25:
        raise ValueError("Observation does not align with the filing fiscal calendar")
    index = report_fy * 4 + quarter - 1 - quarters_back
    fy, q = divmod(index, 4)
    if entry.get("start"):
        span = (end - date.fromisoformat(entry["start"])).days
        if 330 <= span <= 380:
            if q != 3:
                raise ValueError("Annual duration does not end at fiscal year end")
            return fy, FiscalPeriod.FY
        if not 75 <= span <= 105:
            raise ValueError("Not a discrete quarter or full year")
    return fy, FiscalPeriod(f"Q{q + 1}")


def _derive_missing_q4_facts(deduped: dict) -> list[FinancialFact]:
    derived = []
    for concept, fy in sorted({(c, y) for c, y, _ in deduped if c in _DERIVABLE_Q4_CONCEPTS}):
        if (concept, fy, FiscalPeriod.Q4) in deduped:
            continue
        inputs = [deduped.get((concept, fy, p)) for p in (FiscalPeriod.FY, FiscalPeriod.Q1, FiscalPeriod.Q2, FiscalPeriod.Q3)]
        if not all(inputs):
            continue
        annual, q1, q2, q3 = inputs
        if len({f.unit for f in inputs}) != 1:
            continue
        ends = [f.period_end_date for f in (q1, q2, q3, annual)]
        if not all(75 <= (b - a).days <= 105 for a, b in zip(ends, ends[1:])):
            continue
        derived.append(annual.model_copy(update={
            "value": annual.value - q1.value - q2.value - q3.value,
            "fiscal_period": FiscalPeriod.Q4,
            "source_tag": "derived:FY-Q1-Q2-Q3 (" + "; ".join(f"{f.source_tag} {f.accession_number} {f.period_end_date}" for f in inputs) + ")",
        }))
    return derived


def parse_company_facts(raw: dict, cik: str, target_concepts: list[FinancialConcept] | None = None,
                        *, as_of: date | None = None) -> tuple[CompanyFinancials, dict]:
    targets = list(FinancialConcept) if target_concepts is None else target_concepts
    raw = snapshot_company_facts(raw, as_of)
    us_gaap = raw.get("facts", {}).get("us-gaap", {})
    contexts = _contexts(us_gaap)
    deduped = {}
    missing = {}
    for concept in targets:
        resolved = {}
        for tag in TAG_FALLBACKS.get(concept, []):
            per_tag = {}
            for entry in us_gaap.get(tag, {}).get("units", {}).get("USD", []):
                try:
                    if entry.get("form") not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
                        continue
                    if (concept in _DERIVABLE_Q4_CONCEPTS) != bool(entry.get("start")):
                        continue  # duration concepts require a start; balance facts do not
                    fy, fp = _period(entry, contexts[entry["accn"]])
                    value = float(entry["val"])
                    if not math.isfinite(value):
                        raise ValueError("Non-finite financial value")
                    fact = FinancialFact(
                        company_cik=str(int(cik)).zfill(10), concept=concept, value=value,
                        fiscal_year=fy, fiscal_period=fp, period_end_date=date.fromisoformat(entry["end"]),
                        filed_date=date.fromisoformat(entry["filed"]) if entry.get("filed") else None,
                        source=FactSource.XBRL, source_tag=f"us-gaap:{tag}", accession_number=entry["accn"],
                    )
                    key = (concept, fy, fp)
                    old = per_tag.get(key)
                    if old is None or (fact.filed_date or date.min) > (old.filed_date or date.min):
                        per_tag[key] = fact
                except (KeyError, TypeError, ValueError) as exc:
                    logger.debug("Skipping %s observation: %s", tag, exc)
            for key, fact in per_tag.items():
                resolved.setdefault(key, fact)
        deduped.update(resolved)
        if not resolved:
            missing[concept] = TAG_FALLBACKS.get(concept, [])
    for fact in _derive_missing_q4_facts(deduped):
        deduped[(fact.concept, fact.fiscal_year, fact.fiscal_period)] = fact
    return CompanyFinancials(company_cik=str(int(cik)).zfill(10), company_name=raw.get("entityName"),
                             facts=list(deduped.values())), missing

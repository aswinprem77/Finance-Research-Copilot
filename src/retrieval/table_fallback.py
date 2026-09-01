"""
HTML-table fallback extraction — Path B's contribution to the SAME shared
schema Path A (XBRL) populates (src/schema/financial_schema.py). Per PRD v2,
this is used only when XBRL has no tag for a needed figure — so it's
intentionally narrow (best-effort label matching on a known concept list),
not a general table-understanding system. An unmatched row is left
unclassified rather than guessed at: a wrong guess here is worse than a
documented miss (see PRD v2 risk table). Every fact produced here carries
source=HTML_TABLE_FALLBACK so the final memo can flag it for human review.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from src.retrieval.html_ingest import TableBlock
from src.schema.financial_schema import FactSource, FinancialConcept, FinancialFact, FiscalPeriod

# Conservative, explicit label patterns per concept. Expand as real filings
# surface labels this misses — same "grow it as you go" approach as
# TAG_FALLBACKS in xbrl_parser.py.
LABEL_PATTERNS: dict[FinancialConcept, list[str]] = {
    FinancialConcept.REVENUE: [r"\btotal revenue", r"\bnet revenue", r"^revenue[s]?$"],
    FinancialConcept.NET_INCOME: [r"\bnet income", r"\bnet loss"],
    FinancialConcept.GROSS_PROFIT: [r"\bgross profit"],
    FinancialConcept.OPERATING_INCOME: [r"\boperating income", r"\bincome from operations"],
    FinancialConcept.TOTAL_DEBT: [r"\btotal debt", r"\blong.term debt"],
}

_NUMERIC_RE = re.compile(r"^\(?\$?\s*-?[\d,]+(\.\d+)?\)?$")


def _match_concept(label: str) -> Optional[FinancialConcept]:
    normalized = label.strip().lower()
    for concept, patterns in LABEL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, normalized):
                return concept
    return None


def _parse_numeric_cell(cell: str) -> Optional[float]:
    cleaned = cell.strip()
    if not cleaned or not _NUMERIC_RE.match(cleaned):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()$").replace(",", "")
    if not cleaned:
        return None
    value = float(cleaned)
    return -value if negative else value


def extract_facts_from_table(
    table: TableBlock,
    company_cik: str,
    fiscal_year: int,
    fiscal_period: FiscalPeriod,
    period_end_date: date,
) -> list[FinancialFact]:
    """
    For each row, try to match the first cell (the row label) to a known
    concept, then take the FIRST numeric cell among the rest of the row as
    its value — filing tables are typically
    label | current-period | prior-period, so this picks the current
    period's column. fiscal_year/fiscal_period/period_end_date are supplied
    by the caller from the filing's known metadata, not inferred from the
    table itself — header formats vary too much across filers to do that
    reliably here.
    """
    facts: list[FinancialFact] = []
    for row in table.rows:
        if len(row) < 2:
            continue
        label, *value_cells = row
        concept = _match_concept(label)
        if concept is None:
            continue

        value = next((v for v in (_parse_numeric_cell(c) for c in value_cells) if v is not None), None)
        if value is None:
            continue

        facts.append(
            FinancialFact(
                company_cik=company_cik,
                concept=concept,
                value=value,
                unit="USD",
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                period_end_date=period_end_date,
                source=FactSource.HTML_TABLE_FALLBACK,
                source_tag=f"html_table:{label.strip()}",
            )
        )
    return facts

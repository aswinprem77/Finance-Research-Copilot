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
    FinancialConcept.TOTAL_DEBT: [r"^total debt$", r"^total borrowings$"],
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
    """Resolve an explicit dated column and scale; ambiguous tables stay missing.

    Supports flat headers with an English or ISO date and an explicit
    three-month/annual duration for flow concepts. Complex spans need review.
    """
    if not table.rows or table.complex_layout:
        return []
    header = table.rows[0]
    context = " ".join([table.caption or "", *header]).lower()
    scales = [factor for word, factor in (("thousands", 1000), ("millions", 1000000), ("billions", 1000000000)) if word in context]
    if len(scales) > 1 or any(currency in context for currency in ("eur", "euro", "gbp", "cad", "yen")):
        return []
    scale = scales[0] if scales else 1
    date_patterns = [
        period_end_date.isoformat(),
        f"{period_end_date.strftime('%B')} {period_end_date.day}, {period_end_date.year}".lower(),
    ]
    columns = [i for i, cell in enumerate(header) if i > 0 and any(d in cell.lower() for d in date_patterns)]
    expected_duration = ("year ended", "twelve months", "12 months") if fiscal_period == FiscalPeriod.FY else ("three months", "3 months")
    flow_columns = [i for i in columns if any(d in header[i].lower() for d in expected_duration)]
    facts: list[FinancialFact] = []
    for row in table.rows:
        if len(row) < 2:
            continue
        label, *value_cells = row
        concept = _match_concept(label)
        if concept is None:
            continue
        candidates = columns if concept == FinancialConcept.TOTAL_DEBT else flow_columns
        if len(candidates) != 1 or candidates[0] >= len(row):
            continue
        column = candidates[0]
        value = _parse_numeric_cell(row[column])
        if value is None:
            continue

        facts.append(
            FinancialFact(
                company_cik=company_cik,
                concept=concept,
                value=value * scale,
                unit="USD",
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                period_end_date=period_end_date,
                source=FactSource.HTML_TABLE_FALLBACK,
                source_tag=f"html_table:{label.strip()} [section={table.section}; table={table.order}; column={header[column]}; scale={scale}]",
            )
        )
    return facts

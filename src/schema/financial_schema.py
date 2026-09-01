"""
Shared financial data schema.

Per PRD v2 Section 5, Stage 2 splits ingestion into two paths (structured XBRL / Path A,
and unstructured HTML+RAG fallback / Path B), but both paths populate this SAME schema so
downstream stages (Calculator, Judgment, Output) don't need to know which path a fact came
from — except via the `source` field, which is deliberately kept so the final memo can be
honest about which numbers are fully deterministic vs. extracted with more risk.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class FactSource(str, Enum):
    XBRL = "xbrl"
    HTML_TABLE_FALLBACK = "html_table_fallback"


class FiscalPeriod(str, Enum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    FY = "FY"  # full year, used for 10-K annual figures


class FinancialConcept(str, Enum):
    """
    The canonical set of line items the Analyst Agent needs (PRD Section 5, Stage 3).
    Each concept maps to one or more US-GAAP XBRL tags via TAG_FALLBACKS in xbrl_parser.py,
    since filers are not perfectly consistent about which tag they use.
    """
    REVENUE = "revenue"
    NET_INCOME = "net_income"
    GROSS_PROFIT = "gross_profit"
    TOTAL_DEBT = "total_debt"
    OPERATING_INCOME = "operating_income"


class FinancialFact(BaseModel):
    """One data point: a single concept, for a single company, for a single fiscal period."""

    company_cik: str = Field(..., description="SEC Central Index Key, zero-padded to 10 digits")
    company_ticker: Optional[str] = None
    concept: FinancialConcept
    value: float
    unit: str = Field(default="USD")
    fiscal_year: int
    fiscal_period: FiscalPeriod
    period_end_date: date
    filed_date: Optional[date] = None
    source: FactSource
    source_tag: Optional[str] = Field(
        default=None,
        description="The literal XBRL tag (e.g. 'us-gaap:Revenues') or a pointer to the "
                     "source passage/table for html_table_fallback facts.",
    )
    accession_number: Optional[str] = Field(
        default=None, description="SEC accession number of the filing this fact came from."
    )


class CompanyFinancials(BaseModel):
    """All facts collected for one company, across however many periods have been ingested."""

    company_cik: str
    company_ticker: Optional[str] = None
    company_name: Optional[str] = None
    facts: list[FinancialFact] = Field(default_factory=list)

    def facts_for(self, concept: FinancialConcept) -> list[FinancialFact]:
        return [f for f in self.facts if f.concept == concept]

"""
Output Agent — Stage 5, PRD v2 Section 5: "Generates the final memo:
executive summary, key metric changes (table), flagged items with
citations, explicit 'for human review' framing on judgment calls."

STRUCTURAL SCOPE LIMIT, not a prompting instruction: per PRD Section 2
("It does not recommend buy/sell/hold actions"), there is no field
anywhere in this module for a recommendation, and no code path that adds
one. This module has no LLM call in it, same as Calculator and Judgment —
it only ever emits fields it was explicitly given, deterministically.

CAVEAT SURFACING IS A FIRST-CLASS REQUIREMENT HERE, not an afterthought.
Every earlier stage in this project was built "real mechanism, provisional
specifics" — true of the XBRL client (live-untested), the embedding
provider (TF-IDF stand-in), and the rubric thresholds (uncalibrated
defaults). Output is the stage a human actually reads and trusts, so it's
the one place in the pipeline where omitting that context — even just by
not printing it — would actively mislead the reader rather than just being
incomplete. generate_memo() REQUIRES a data_provenance_note; there's no
default, so it can't be silently skipped. See PROGRESS.md for why this was
made a hard requirement rather than a documentation convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.analyst.calculator import ComparisonResult
from src.judgment.rubric import RUBRIC_VERSION, Flag
from src.schema.financial_schema import CompanyFinancials, FactSource, FinancialConcept, FiscalPeriod

SCOPE_DISCLAIMER = (
    "This is a decision-support summary, not an investment recommendation. "
    "It does not and will not suggest a buy/sell/hold action (PRD Section 2)."
)


@dataclass
class MetricRow:
    concept: str
    period_label: str
    comparison_label: str
    current_value: float
    percent_change: Optional[float]
    provenance: str  # "xbrl" | "derived" | "html_table_fallback" | "unknown"


@dataclass
class Memo:
    company_name: str
    company_cik: str
    rubric_version: str
    data_provenance_note: str
    executive_summary: list[str]
    metric_rows: list[MetricRow]
    flags: list[Flag] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Filing Research Memo — {self.company_name} ({self.company_cik})",
            "",
            f"**Data basis:** {self.data_provenance_note}",
            f"**Rubric version:** {self.rubric_version}",
            "",
            f"> {SCOPE_DISCLAIMER}",
            "",
            "## Executive Summary",
        ]
        lines += [f"- {s}" for s in self.executive_summary]

        lines += [
            "",
            "## Key Metric Changes",
            "",
            "| Metric | Period | Comparison | Value | Change | Source |",
            "|---|---|---|---|---|---|",
        ]
        provenance_marker = {
            "xbrl": "XBRL",
            "derived": "Derived",
            "html_table_fallback": "**HTML fallback — human review**",
            "unknown": "Unknown",
        }
        for r in self.metric_rows:
            change_str = f"{r.percent_change:+.2f}%" if r.percent_change is not None else "N/A"
            lines.append(
                f"| {r.concept} | {r.period_label} | {r.comparison_label} | "
                f"${r.current_value:,.0f} | {change_str} | {provenance_marker.get(r.provenance, r.provenance)} |"
            )

        lines += ["", "## Flagged Items"]
        if not self.flags:
            lines.append("_(none — nothing met the current rubric's thresholds)_")
        for f in self.flags:
            lines += [
                "",
                f"**[{f.rule_id}]** {f.detail}",
                f"- Cites: {f.citation}",
                "- For human review: yes — every Judgment agent flag is a screen, not a verdict (PRD Section 5).",
            ]

        return "\n".join(lines)


def _provenance_for(
    financials: CompanyFinancials, concept: FinancialConcept, fiscal_year: int, fiscal_period: FiscalPeriod
) -> str:
    fact = next(
        (
            f for f in financials.facts_for(concept)
            if f.fiscal_year == fiscal_year and f.fiscal_period == fiscal_period
        ),
        None,
    )
    if fact is None:
        return "unknown"
    if fact.source == FactSource.HTML_TABLE_FALLBACK:
        return "html_table_fallback"
    if fact.source_tag and fact.source_tag.startswith("derived:"):
        return "derived"
    return "xbrl"


def build_metric_rows(
    financials: CompanyFinancials, comparison_results: list[ComparisonResult]
) -> list[MetricRow]:
    """Turns Calculator output into memo rows, looking up each row's actual data
    provenance from the underlying facts (ComparisonResult itself doesn't carry
    source/source_tag — ComparisonResult is is a Calculator concept, provenance
    is a schema/ingestion concept, and this function is the seam between them)."""
    rows = []
    for r in comparison_results:
        provenance = _provenance_for(financials, r.concept, r.current_fiscal_year, r.current_period)
        rows.append(
            MetricRow(
                concept=r.concept.value,
                period_label=f"{r.current_period.value} FY{r.current_fiscal_year}",
                comparison_label=f"vs {r.comparison_period.value} FY{r.comparison_fiscal_year} ({r.comparison_type})",
                current_value=r.current_value,
                percent_change=r.percent_change,
                provenance=provenance,
            )
        )
    return rows


def _build_executive_summary(metric_rows: list[MetricRow], flags: list[Flag]) -> list[str]:
    """Deterministic, template-based summary lines — not LLM-generated free text,
    consistent with every other stage in this project having no LLM in the loop."""
    summary = []
    notable_flags = [f for f in flags if f.severity == "notable"]
    if notable_flags:
        summary.append(f"{len(notable_flags)} item(s) flagged for review this period.")
    else:
        summary.append("No items met the current rubric's flagging thresholds this period.")

    fallback_count = sum(1 for r in metric_rows if r.provenance == "html_table_fallback")
    if fallback_count:
        summary.append(
            f"{fallback_count} figure(s) below came from HTML-table fallback extraction, not XBRL "
            "— flagged for human review, not fully deterministic."
        )

    derived_count = sum(1 for r in metric_rows if r.provenance == "derived")
    if derived_count:
        summary.append(
            f"{derived_count} figure(s) below are derived (e.g. Q4 = FY - Q1 - Q2 - Q3), "
            "not directly reported by the filer."
        )

    return summary


def generate_memo(
    financials: CompanyFinancials,
    comparison_results: list[ComparisonResult],
    flags: list[Flag],
    data_provenance_note: str,
    rubric_version: str = RUBRIC_VERSION,
) -> Memo:
    """
    `data_provenance_note` is REQUIRED — no default value. Forces every caller to
    state plainly what this memo's numbers are actually based on, e.g.
    "Real XBRL data for NVDA (CIK 0001045810), fetched 2026-08-10" vs.
    "SYNTHETIC TEST FIXTURE — not a real company, for pipeline demonstration only".
    This can't be forgotten or defaulted away; the function signature won't allow it.
    """
    metric_rows = build_metric_rows(financials, comparison_results)
    executive_summary = _build_executive_summary(metric_rows, flags)

    return Memo(
        company_name=financials.company_name or financials.company_cik,
        company_cik=financials.company_cik,
        rubric_version=rubric_version,
        data_provenance_note=data_provenance_note,
        executive_summary=executive_summary,
        metric_rows=metric_rows,
        flags=flags,
    )

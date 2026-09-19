"""
Judgment Agent — Stage 4, PRD v2 Section 5.

Applies an explicit, documented, versioned rubric to classify findings as
notable vs. routine. Per PRD: "Every flag must cite the specific source
passage and the rubric rule that triggered it (no unexplained 'AI thinks
this is important')." That's enforced structurally here, not just by
convention — Flag is a plain dataclass with required rule_id and citation
fields; there's no code path that produces a flag without both.

RUBRIC THRESHOLDS BELOW ARE REASONABLE DEFAULTS, NOT CALIBRATED AGAINST
REAL FILINGS — same "real mechanism, provisional specifics" pattern used
for the embedding provider in Phase 2. The mechanism (this file) doesn't
need to change to recalibrate; only the threshold constants do, once real
data shows what "routine" actually looks like for the watchlist sector.
See PROGRESS.md.

Per PRD risk table: "Rubric is versioned and testable; track false-
positive/false-negative rate against a manually labeled sample set." This
file's rules are unit-tested against synthetic scenarios (this session);
false-positive/negative RATE tracking against a real labeled sample is
still ahead — needs real flagged output to label against first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Optional

from src.analyst.calculator import ComparisonResult, compute_margin
from src.ingestion.xbrl_parser import TAG_FALLBACKS
from src.retrieval.chunking import Chunk
from src.schema.financial_schema import CompanyFinancials, FinancialConcept
from src.schema.citations import fact_citation

RUBRIC_VERSION = "v2"

# Percentage-POINT threshold for margin changes (e.g. gross margin moving from 40% to 35%
# is a 5.0 point change, regardless of what percent that represents of the starting margin).
MARGIN_CHANGE_THRESHOLD_PP = 5.0

# Percent threshold for raw metric changes (revenue, net income, etc.), YoY or QoQ.
METRIC_CHANGE_THRESHOLD_PCT = 20.0

# Conservative, explicit keyword list -- same "narrow and grow it" philosophy as
# LABEL_PATTERNS in table_fallback.py. A miss here is a documented gap; a false flag
# from an overly broad keyword is worse (erodes trust in every other flag).
LITIGATION_KEYWORDS = [
    "lawsuit", "litigation", "complaint", "alleging", "breach of contract",
    "class action", "subpoena", "indictment", "settlement agreement",
]


@dataclass
class Flag:
    rule_id: str
    rule_description: str
    severity: str  # "notable" | "routine" -- PRD's own vocabulary (Section 5, Stage 4)
    citation: str  # human-readable pointer to the specific source fact/passage
    detail: str  # the specific finding, in plain language


def flag_metric_changes(
    comparison_results: list[ComparisonResult],
    threshold_pct: float = METRIC_CHANGE_THRESHOLD_PCT,
    financials: CompanyFinancials | None = None,
) -> list[Flag]:
    """Flags a YoY or QoQ ComparisonResult (from calculator.py) whose percent change
    exceeds threshold_pct. Skips results where percent_change is None (e.g. prior
    value was 0 — calculator.py already guards div-by-zero)."""
    flags = []
    for r in comparison_results:
        if r.percent_change is None or abs(r.percent_change) < threshold_pct:
            continue
        sources = [] if financials is None else [fact_citation(f) for f in financials.facts_for(r.concept)
            if (f.fiscal_year, f.fiscal_period) in ((r.current_fiscal_year, r.current_period), (r.comparison_fiscal_year, r.comparison_period))]
        flags.append(
            Flag(
                rule_id="METRIC_CHANGE_THRESHOLD",
                rule_description=f"{r.comparison_type} change in {r.concept.value} >= {threshold_pct}%",
                severity="notable",
                citation=(
                    f"{r.concept.value}: {r.current_period.value} FY{r.current_fiscal_year} "
                    f"vs {r.comparison_period.value} FY{r.comparison_fiscal_year}; " + "; ".join(sources)
                ),
                detail=(
                    f"{r.concept.value} changed {r.percent_change:+.2f}% ({r.comparison_type}): "
                    f"${r.comparison_value:,.0f} -> ${r.current_value:,.0f}"
                ),
            )
        )
    return flags


def flag_margin_changes(
    financials: CompanyFinancials,
    numerator: FinancialConcept,
    denominator: FinancialConcept = FinancialConcept.REVENUE,
    threshold_pp: float = MARGIN_CHANGE_THRESHOLD_PP,
    current_period: tuple | None = None,
) -> list[Flag]:
    """Flags a YoY margin move of >= threshold_pp PERCENTAGE POINTS (not percent --
    see MARGIN_CHANGE_THRESHOLD_PP docstring above)."""
    margins = compute_margin(financials, numerator, denominator)
    flags = []
    for (fy, fp), current in margins.items():
        if current_period is not None and (fy, fp) != current_period:
            continue
        if current is None:
            continue
        prior = margins.get((fy - 1, fp))
        if prior is None:
            continue
        change_pp = current - prior
        if abs(change_pp) < threshold_pp:
            continue
        flags.append(
            Flag(
                rule_id="MARGIN_THRESHOLD",
                rule_description=f"YoY {numerator.value}/{denominator.value} margin change >= {threshold_pp}pp",
                severity="notable",
                citation=f"{numerator.value}/{denominator.value} margin, {fp.value} FY{fy} vs FY{fy - 1}; " + "; ".join(
                    fact_citation(f) for f in financials.facts if f.concept in (numerator, denominator)
                    and f.fiscal_period == fp and f.fiscal_year in (fy, fy - 1)),
                detail=f"Margin moved {change_pp:+.2f}pp: {prior:.2f}% -> {current:.2f}%",
            )
        )
    return flags


def detect_restatements(raw: dict, target_concepts: list[FinancialConcept], *, accession_number: str | None = None) -> list[Flag]:
    """
    Compare the same tag/unit/start/end across filings, independently of fy/fp.
    Quarter and YTD durations are separate observations. The normalized parser
    keeps only the latest revision, so this pass uses the raw observations.
    Differences are candidate restatements for review, not proven corrections.
    """
    us_gaap = raw.get("facts", {}).get("us-gaap", {})
    flags: list[Flag] = []

    for concept in target_concepts:
        resolved_tag = next((t for t in TAG_FALLBACKS.get(concept, []) if t in us_gaap), None)
        if resolved_tag is None:
            continue

        entries = us_gaap[resolved_tag].get("units", {}).get("USD", [])
        by_period: dict[tuple, dict[str, float]] = {}
        for entry in entries:
            try:
                end = date.fromisoformat(entry["end"])
                start = date.fromisoformat(entry["start"]) if entry.get("start") else None
                value = float(entry["val"])
                if not math.isfinite(value):
                    continue
                key = (start, end)
                by_period.setdefault(key, {})[entry["accn"]] = value
            except (KeyError, TypeError, ValueError):
                continue

        for (start, end), accn_values in by_period.items():
            if accession_number is not None and accession_number not in accn_values:
                continue
            distinct_values = sorted(set(accn_values.values()))
            if len(distinct_values) <= 1:
                continue
            flags.append(
                Flag(
                    rule_id="POSSIBLE_RESTATEMENT",
                    rule_description="Same concept/period reported with different values across filings",
                    severity="notable",
                    citation=f"us-gaap:{resolved_tag} USD {start or 'instant'} to {end}, accessions: {sorted(accn_values.keys())}",
                    detail=f"{concept.value} {start or 'instant'} to {end} reported as {distinct_values} across different filings",
                )
            )
    return flags


def flag_litigation_language(chunks: list[Chunk], keywords: Optional[list[str]] = None) -> list[Flag]:
    """Flags prose chunks (never tables — see Chunk.kind) containing litigation-indicating
    keywords. Deliberately keyword-based, not NLP classification, per the same
    "no unexplained AI judgment" requirement this whole module exists to satisfy — a
    keyword match is explainable; a model's internal judgment call isn't."""
    active_keywords = keywords if keywords is not None else LITIGATION_KEYWORDS
    flags = []
    for chunk in chunks:
        if chunk.kind != "prose":
            continue
        text_lower = chunk.text.lower()
        matched = [kw for kw in active_keywords if kw in text_lower]
        if not matched:
            continue
        position = min(text_lower.index(kw) for kw in matched)
        start = max(0, position - 100)
        preview = ("..." if start else "") + chunk.text[start:start + 400] + ("..." if len(chunk.text) > start + 400 else "")
        flags.append(
            Flag(
                rule_id="LITIGATION_LANGUAGE",
                rule_description=f"Prose contains litigation-indicating language: {matched}",
                severity="notable",
                citation=f"{chunk.section} ({chunk.chunk_id})",
                detail=preview,
            )
        )
    return flags

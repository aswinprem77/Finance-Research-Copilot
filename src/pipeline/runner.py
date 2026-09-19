"""Single-worker filing state machine: detect -> ingest -> analyze -> save -> acknowledge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import os
import re
import tempfile
import time
from typing import Callable

from src.analyst.calculator import compute_qoq, compute_yoy
from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import TAG_FALLBACKS, parse_company_facts, snapshot_company_facts
from src.judgment.rubric import Flag, detect_restatements, flag_litigation_language, flag_margin_changes, flag_metric_changes
from src.output.memo import Memo, generate_memo
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.pipeline.watchlist import Watchlist
from src.retrieval.chunking import chunk_blocks
from src.retrieval.embeddings import TfidfEmbeddingProvider
from src.retrieval.html_ingest import parse_filing_html
from src.retrieval.hybrid_index import HybridIndex
from src.retrieval.rerank import rerank_lexical_overlap
from src.schema.financial_schema import CompanyFinancials, FinancialConcept, FiscalPeriod
from src.trigger.edgar_client import FilingEvent, parse_recent_filings
from src.trigger.state_store import load_seen_accessions, save_seen_accessions


@dataclass
class FilingResult:
    event: FilingEvent
    memo: Memo
    coverage_pct: float
    missing_concepts: list[FinancialConcept]
    elapsed_seconds: float


@dataclass
class PollResult:
    completed: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def process_filing(event: FilingEvent, fetch_companyfacts: Callable, fetch_html: Callable,
                   *, data_provenance_note: str) -> FilingResult:
    started = time.monotonic()
    periodic = event.form.replace("/A", "") in {"10-K", "10-Q"}

    def structured():
        if not periodic:
            return {}, CompanyFinancials(company_cik=event.company_cik, company_name=event.company_name)
        raw = snapshot_company_facts(fetch_companyfacts(event.company_cik), event.filing_date)
        if raw.get("cik") is not None and int(raw["cik"]) != int(event.company_cik):
            raise ValueError("Companyfacts CIK does not match the filing")
        parsed, _ = parse_company_facts(raw, event.company_cik)
        return raw, parsed

    def unstructured():
        html = fetch_html(event)
        if not html.strip():
            raise ValueError("Empty filing HTML")
        return html, chunk_blocks(parse_filing_html(html))

    with ThreadPoolExecutor(max_workers=2) as pool:
        path_a = pool.submit(structured)
        path_b = pool.submit(unstructured)
        raw, financials = path_a.result()
        html, chunks = path_b.result()

    targets = list(FinancialConcept)
    period = None
    comparisons = []
    flags = []
    gaps = targets
    coverage_pct = 0.0
    if periodic:
        current_facts = [f for f in financials.facts if f.accession_number == event.accession_number
                         and f.period_end_date == event.report_date]
        preferred = FiscalPeriod.FY if event.form.startswith("10-K") else None
        candidates = {(f.fiscal_year, f.fiscal_period) for f in current_facts
                      if f.concept != FinancialConcept.TOTAL_DEBT and (preferred is None or f.fiscal_period == preferred)}
        if len(candidates) != 1:
            raise ValueError("Filing-specific XBRL period unavailable or ambiguous; retry after SEC data refresh")
        period = next(iter(candidates))
        # A year-end debt balance belongs beside annual flows without summing it.
        if period[1] == FiscalPeriod.FY:
            annual_debt = [f.model_copy(update={"fiscal_period": FiscalPeriod.FY}) for f in financials.facts
                           if f.concept == FinancialConcept.TOTAL_DEBT and f.fiscal_period == FiscalPeriod.Q4]
            financials.facts.extend(annual_debt)
        available = {f.concept for f in financials.facts if (f.fiscal_year, f.fiscal_period) == period
                     and f.accession_number == event.accession_number and f.period_end_date == event.report_date}
        coverage = compute_coverage(event.company_cik, targets, {c: TAG_FALLBACKS[c] for c in targets if c not in available})
        coverage_pct = coverage.coverage_rate_pct
        # Do not reuse a stale fact for the event's current period.
        financials.facts = [f for f in financials.facts if (f.fiscal_year, f.fiscal_period) != period or f.concept in available]
        filled = fill_coverage_gaps_from_html(financials, coverage, html, *period, event.report_date,
                                             accession_number=event.accession_number, filed_date=event.filing_date)
        financials, gaps = filled.financials, filled.still_missing
        for concept in targets:
            comparisons.extend(r for r in compute_yoy(financials, concept) + compute_qoq(financials, concept)
                               if (r.current_fiscal_year, r.current_period) == period)
        flags.extend(flag_metric_changes(comparisons, financials=financials))
        for numerator in (FinancialConcept.NET_INCOME, FinancialConcept.GROSS_PROFIT, FinancialConcept.OPERATING_INCOME):
            flags.extend(flag_margin_changes(financials, numerator, current_period=period))
        flags.extend(detect_restatements(raw, targets, accession_number=event.accession_number))

    if chunks:
        index = HybridIndex(TfidfEmbeddingProvider())
        try:
            index.build(chunks)
            evidence = {}
            for query in ("lawsuit litigation regulatory investigation", "debt covenant liquidity default going concern"):
                for hit in rerank_lexical_overlap(query, index.search(query), top_k=5):
                    evidence[hit.chunk.chunk_id] = hit.chunk
            flags.extend(flag_litigation_language(list(evidence.values())))
            for chunk in evidence.values():
                terms = [t for t in ("covenant", "liquidity", "going concern", "default", "regulatory investigation") if t in chunk.text.lower()]
                if chunk.kind == "prose" and terms:
                    flags.append(Flag("LIQUIDITY_REGULATORY_LANGUAGE", f"Passage contains {terms}", "notable",
                                      f"{chunk.section} ({chunk.chunk_id})", chunk.text[:400]))
        finally:
            index.close()
    for flag in flags:
        if flag.rule_id in {"LITIGATION_LANGUAGE", "LIQUIDITY_REGULATORY_LANGUAGE"}:
            accession = event.accession_number
            url = f"https://www.sec.gov/Archives/edgar/data/{int(event.company_cik)}/{accession.replace('-', '')}/{accession}-index.html"
            flag.citation += f"; [{accession}]({url})"
    note = (f"{data_provenance_note} Filing {event.accession_number} ({event.form}), filed {event.filing_date}. "
            "Retrieval uses TF-IDF and lexical reranking; rubric thresholds are uncalibrated. "
            "Language flags identify mentions, not verified changes from a prior filing. Images/charts are not processed.")
    memo = generate_memo(financials, comparisons, flags, note, current_period=period)
    if periodic:
        memo.executive_summary.append(f"Current-period XBRL coverage: {coverage_pct:.1f}% ({len(targets)} required concepts).")
        if gaps:
            memo.executive_summary.append("Unresolved figures requiring review: " + ", ".join(c.value for c in gaps) + ".")
    else:
        memo.executive_summary.append("Event filing: narrative screening only; periodic financial comparisons are unavailable.")
    if not chunks:
        memo.executive_summary.append("No supported prose/table blocks extracted; filing requires manual review.")
    return FilingResult(event, memo, coverage_pct, gaps, time.monotonic() - started)


def write_memo(result: FilingResult, output_dir: Path | str) -> Path:
    accession = result.event.accession_number
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("Invalid SEC accession number")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{accession}.md"
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(result.memo.to_markdown())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def run_poll_cycle(watchlist: Watchlist, fetch_submissions: Callable, fetch_companyfacts: Callable,
                   fetch_html: Callable, *, state_path: Path | str, output_dir: Path | str,
                   data_provenance_note: str, since: date | None = None) -> PollResult:
    """Acknowledge only after durable output. One failing company/filing cannot stop others.

    One process must own a state file. Filesystem replacement protects against
    interrupted writes, not concurrent workers; use a database queue for those.
    """
    seen = load_seen_accessions(state_path)
    result = PollResult()
    for company in watchlist.companies:
        try:
            events = parse_recent_filings(fetch_submissions(company.cik), company.cik)
        except Exception as exc:
            result.errors[company.cik] = str(exc)
            continue
        for event in sorted(events, key=lambda e: (e.filing_date, e.accession_number)):
            if event.accession_number in seen or (since and event.filing_date < since):
                continue
            try:
                filing = process_filing(event, fetch_companyfacts, fetch_html, data_provenance_note=data_provenance_note)
                path = write_memo(filing, output_dir)
                updated = seen | {event.accession_number}
                save_seen_accessions(updated, state_path)
                seen = updated
                result.completed.append(path)
            except Exception as exc:
                result.errors[event.accession_number] = str(exc)
    return result

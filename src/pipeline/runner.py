"""Single-worker filing state machine: detect -> ingest -> analyze -> save -> acknowledge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
import os
import re
import tempfile
import time
from typing import Callable

from src.analyst.calculator import compute_qoq, compute_yoy
from src.analyst.peers import PEER_CONCEPTS, PeerComparison, build_peer_comparison
from src.ingestion.coverage import compute_coverage
from src.ingestion.xbrl_parser import TAG_FALLBACKS, parse_company_facts, snapshot_company_facts
from src.judgment.narrative import FilingNarrative, compare_narrative, flag_narrative_changes, screen_filing_narrative
from src.judgment.narrative_store import load_prior_narrative, save_narrative
from src.judgment.rubric import detect_restatements, flag_margin_changes, flag_metric_changes
from src.output.memo import Memo, generate_memo
from src.pipeline.stage2_gap_fill import fill_coverage_gaps_from_html
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.retrieval.hybrid_index import HybridIndex
from src.retrieval.providers import RetrievalStack, build_retrieval_stack
from src.service.notify import NullNotifier, Notifier, deliver
from src.service.records import build_run_record, save_run_record
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
    # Screened narrative for this filing. The caller persists it after the
    # memo is durable, so a failed run leaves no baseline the next filing
    # would compare against.
    narrative: FilingNarrative | None = None
    prior_narrative_accession: str | None = None
    peer_comparison: PeerComparison | None = None


@dataclass
class PollResult:
    completed: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # Separate from errors: the filing succeeded, only the alert did not.
    delivery_failures: dict[str, str] = field(default_factory=dict)


def _narrative_note(narrative: FilingNarrative | None, prior: FilingNarrative | None) -> str:
    """State plainly what the language flags on this memo are and are not based on."""
    if narrative is None:
        return "No narrative was screened for this filing."
    if prior is None or not prior.passages:
        return ("Language flags list screened passages only: no prior filing is on record for this "
                "company, so none of them has been established as new or changed.")
    return (f"Language flags compare screened passages against {prior.accession_number} "
            f"(filed {prior.filing_date}) by word-level similarity with dates normalized. "
            "Thresholds are uncalibrated and a reworded passage can read as new.")


def _build_peers(event: FilingEvent, peers: list[WatchlistCompany], fetch_peer_facts: Callable,
                 subject: CompanyFinancials, period: tuple) -> PeerComparison | None:
    """
    Assemble the peer set for this filing.

    One peer failing must not cost the memo its other peers, or the filing
    itself, so every fetch is isolated and its reason is carried into the
    table as a named gap. Peer facts are snapshotted to the subject's filing
    date for the same reason the subject's are: a peer figure first published
    after this filing was not available to anyone reading it.
    """
    report_date = event.report_date
    if report_date is None:
        return None
    parsed: list[CompanyFinancials] = []
    unavailable: dict[str, str] = {}
    for company in peers:
        if company.cik == event.company_cik:
            continue
        try:
            raw = snapshot_company_facts(fetch_peer_facts(company.cik), event.filing_date)
            financials, _ = parse_company_facts(raw, company.cik, list(PEER_CONCEPTS))
            financials.company_name = company.name
            financials.company_ticker = company.ticker
            parsed.append(financials)
        except Exception as exc:
            unavailable[company.cik] = f"peer data unavailable ({exc})"
            parsed.append(CompanyFinancials(company_cik=company.cik, company_name=company.name,
                                            company_ticker=company.ticker))
    if not parsed:
        return None
    return build_peer_comparison(subject, parsed, fiscal_year=period[0], fiscal_period=period[1],
                                 period_end_date=report_date, unavailable=unavailable)


def process_filing(event: FilingEvent, fetch_companyfacts: Callable, fetch_html: Callable,
                   *, data_provenance_note: str, retrieval: RetrievalStack | None = None,
                   narrative_dir: Path | str | None = None,
                   peers: list[WatchlistCompany] | None = None,
                   fetch_peer_facts: Callable | None = None) -> FilingResult:
    started = time.monotonic()
    retrieval = retrieval or build_retrieval_stack()
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
    peer_comparison = None
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
        if peers and fetch_peer_facts is not None:
            peer_comparison = _build_peers(event, peers, fetch_peer_facts, financials, period)

    narrative = None
    prior = None
    if chunks:
        # Screening runs over every prose chunk, deterministically. Retrieval
        # decides what evidence a memo surfaces; it must not decide what gets
        # screened, or a disclosure outside the top-k would never be seen.
        narrative = screen_filing_narrative(
            chunks, company_cik=event.company_cik, accession_number=event.accession_number,
            form=event.form, filing_date=event.filing_date, report_date=event.report_date,
        )
        if narrative_dir is not None:
            # Strictly earlier filings only, so reprocessing an old filing
            # cannot compare it against one that did not exist yet.
            prior = load_prior_narrative(narrative_dir, event.company_cik,
                                         before=event.filing_date,
                                         exclude_accession=event.accession_number)
        # Path B ranks the screened passages. It only breaks ties within a
        # status, so it can reorder a capped list but never suppress a flag.
        relevance_order: list[str] = []
        index = HybridIndex(retrieval.embeddings)
        try:
            index.build(chunks)
            for query in ("lawsuit litigation regulatory investigation", "debt covenant liquidity default going concern"):
                for hit in retrieval.rerank(query, index.search(query), top_k=5):
                    if hit.chunk.chunk_id not in relevance_order:
                        relevance_order.append(hit.chunk.chunk_id)
        finally:
            index.close()
        flags.extend(flag_narrative_changes(compare_narrative(narrative, prior), prior,
                                            relevance_order=relevance_order))
    for flag in flags:
        if flag.rule_id.endswith("_LANGUAGE"):
            accession = event.accession_number
            url = f"https://www.sec.gov/Archives/edgar/data/{int(event.company_cik)}/{accession.replace('-', '')}/{accession}-index.html"
            flag.citation += f"; [{accession}]({url})"
    note = (f"{data_provenance_note} Filing {event.accession_number} ({event.form}), filed {event.filing_date}. "
            f"{retrieval.describe()} Rubric thresholds are uncalibrated. "
            f"{_narrative_note(narrative, prior)} Images/charts are not processed.")
    memo = generate_memo(financials, comparisons, flags, note, current_period=period,
                         peer_comparison=peer_comparison)
    if periodic:
        memo.executive_summary.append(f"Current-period XBRL coverage: {coverage_pct:.1f}% ({len(targets)} required concepts).")
        if gaps:
            memo.executive_summary.append("Unresolved figures requiring review: " + ", ".join(c.value for c in gaps) + ".")
    else:
        memo.executive_summary.append("Event filing: narrative screening only; periodic financial comparisons are unavailable.")
    if not chunks:
        memo.executive_summary.append("No supported prose/table blocks extracted; filing requires manual review.")
    return FilingResult(event, memo, coverage_pct, gaps, time.monotonic() - started,
                        narrative=narrative,
                        prior_narrative_accession=prior.accession_number if prior else None,
                        peer_comparison=peer_comparison)


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
                   data_provenance_note: str, since: date | None = None,
                   retrieval: RetrievalStack | None = None,
                   narrative_dir: Path | str | None = None,
                   peer_comparisons: bool = True,
                   records_dir: Path | str | None = None,
                   notifier: Notifier | None = None) -> PollResult:
    """Acknowledge only after durable output. One failing company/filing cannot stop others.

    One process must own a state file. Filesystem replacement protects against
    interrupted writes, not concurrent workers; use a database queue for those.
    """
    # Built once per cycle, not per filing: the stack holds the loaded encoder,
    # and reloading model weights for every filing would dominate the run.
    retrieval = retrieval or build_retrieval_stack()
    # Companyfacts documents are large and every filing in a cycle compares
    # against the same peers, so each peer is fetched at most once per cycle.
    peer_cache: dict[str, dict] = {}

    def peer_facts(cik: str) -> dict:
        if cik not in peer_cache:
            peer_cache[cik] = fetch_companyfacts(cik)
        return peer_cache[cik]

    peers = watchlist.companies if peer_comparisons else None
    notifier = notifier or NullNotifier()
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
                filing = process_filing(event, fetch_companyfacts, fetch_html,
                                        data_provenance_note=data_provenance_note, retrieval=retrieval,
                                        narrative_dir=narrative_dir, peers=peers,
                                        fetch_peer_facts=peer_facts if peers else None)
                path = write_memo(filing, output_dir)
                # Only after the memo is durable, and before acknowledging, so a
                # failed run leaves neither a memo nor a baseline behind.
                if narrative_dir is not None and filing.narrative is not None:
                    save_narrative(filing.narrative, narrative_dir)
                if records_dir is not None:
                    record = build_run_record(filing, memo_filename=path.name,
                                              company_ticker=company.ticker)
                    # Written before delivery is attempted, so a crash mid-send
                    # leaves a record marked pending that --notify-pending can
                    # retry without reprocessing the filing.
                    save_run_record(record, records_dir)
                    status, error = deliver(record, notifier)
                    record.delivery_status, record.delivery_error = status, error
                    if status == "sent":
                        record.delivered_at = datetime.now(timezone.utc).isoformat()
                    save_run_record(record, records_dir)
                    if status == "failed":
                        # Surfaced, but never fatal: the memo is the deliverable
                        # and the accession is still acknowledged below.
                        result.delivery_failures[event.accession_number] = error
                updated = seen | {event.accession_number}
                save_seen_accessions(updated, state_path)
                seen = updated
                result.completed.append(path)
            except Exception as exc:
                result.errors[event.accession_number] = str(exc)
    return result

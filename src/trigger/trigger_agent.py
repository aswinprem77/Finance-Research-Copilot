"""
Trigger Agent — Stage 1, PRD v2 Section 5: "Polls SEC EDGAR's filing index
on a schedule for new filings from a defined watchlist... On detection,
kicks off the pipeline for that filing."

This legacy utility acknowledges detection immediately and is retained for
the stage-level demo. The automatic worker in src.pipeline.runner uses
success-only completion state instead; use that worker for memo delivery.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.pipeline.watchlist import Watchlist
from src.trigger.edgar_client import FilingEvent, parse_recent_filings
from src.trigger.state_store import DEFAULT_STATE_PATH, load_seen_accessions, save_seen_accessions


def poll_watchlist_for_new_filings(
    watchlist: Watchlist,
    fetch_submissions_fn: Callable[[str], dict],
    state_path: Path | str = DEFAULT_STATE_PATH,
) -> list[FilingEvent]:
    """
    For each company in the watchlist, calls `fetch_submissions_fn(cik)` to get
    raw submissions JSON -- injected rather than hardcoded to
    edgar_client.fetch_submissions so this is testable offline (production
    passes the real client function, tests pass a fixture-returning stand-in),
    same dependency-injection pattern as watchlist.py's run_watchlist_coverage().

    Returns only filings whose accession number ISN'T already in the persisted
    seen-state, then updates that state to include everything returned -- a
    second call against the same underlying data returns nothing.
    """
    seen = load_seen_accessions(state_path)
    new_events: list[FilingEvent] = []

    for company in watchlist.companies:
        raw = fetch_submissions_fn(company.cik)
        for filing in parse_recent_filings(raw, cik=company.cik):
            if filing.accession_number not in seen:
                new_events.append(filing)
                seen.add(filing.accession_number)

    save_seen_accessions(seen, state_path)
    return new_events

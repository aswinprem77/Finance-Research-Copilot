"""
Seed narrative baselines for filings that predate this installation.

Narrative change detection compares a filing against the same company's
previous one, and a baseline exists only for filings the poller processed
while it was running. On a fresh install nothing has been processed, so the
poller finds at most one filing per company inside its window, has nothing to
compare it against, and reports every screened passage as `unestablished`.
That state persists until each company has filed twice under a running system
- roughly a quarter for a 10-Q watchlist. The feature is inert exactly when a
new user is deciding whether it works.

Backfill closes that gap: fetch each company's most recent past filing, screen
it, and store the baseline, so the next real filing is compared rather than
merely listed.

What it deliberately does NOT do:

- No memos. A backfilled filing was already public before this installation
  existed; generating research memos for it would imply the system had
  analysed it at the time.
- No completion state. Marking those accessions seen would make the poller
  skip them if they later fall inside its window, so a filing that arrives
  during setup would silently never get a memo.
- No XBRL. Screening reads prose only, so seeding costs one HTML fetch per
  company rather than a full companyfacts download. That is what makes
  seeding the whole watchlist cheap enough to be the default first step.

`filings.recent` is sufficient here despite the pagination gap noted in
edgar_client: it covers roughly the last thousand filings, which for any of
these companies reaches back years. Only the single nearest prior filing is
ever consulted by a comparison, so depth 1 is enough; greater depth exists for
reprocessing history, not for correctness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from src.judgment.narrative import screen_filing_narrative
from src.judgment.narrative_store import save_narrative, stored_accessions
from src.pipeline.watchlist import Watchlist
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.trigger.edgar_client import FilingEvent, parse_recent_filings

PERIODIC_FORMS = {"10-K", "10-Q"}


@dataclass
class BackfillResult:
    seeded: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def seeded_count(self) -> int:
        return sum(len(v) for v in self.seeded.values())

    def to_dict(self) -> dict:
        return {"seeded": self.seeded, "seeded_count": self.seeded_count,
                "skipped": self.skipped, "errors": self.errors}


def periodic_filings_before(
    submissions: dict, cik: str, *, before: date | None = None
) -> list[FilingEvent]:
    """
    This company's 10-K/10-Q filings strictly before `before`, newest first.

    Amendments are excluded: an amended filing restates one already covered,
    and seeding from it would make the next comparison run against a revision
    rather than against what the company originally said.
    """
    events = [e for e in parse_recent_filings(submissions, cik) if e.form in PERIODIC_FORMS]
    if before is not None:
        events = [e for e in events if e.filing_date < before]
    return sorted(events, key=lambda e: (e.filing_date, e.accession_number), reverse=True)


def backfill_narrative_baselines(
    watchlist: Watchlist,
    fetch_submissions: Callable[[str], dict],
    fetch_html: Callable[[FilingEvent], str],
    *,
    narrative_dir: Path | str,
    before: date | None = None,
    depth: int = 1,
    force: bool = False,
) -> BackfillResult:
    """
    Seed up to `depth` baselines per company.

    A company that already has any baseline is skipped unless `force`, so
    re-running is cheap and does not refetch what it has. One company failing
    does not stop the others, matching the poller's isolation rule.
    """
    if depth < 1:
        raise ValueError("depth must be at least 1")
    result = BackfillResult()

    for company in watchlist.companies:
        try:
            existing = stored_accessions(narrative_dir, company.cik)
            if existing and not force:
                result.skipped[company.cik] = f"{len(existing)} baseline(s) already stored"
                continue

            candidates = periodic_filings_before(fetch_submissions(company.cik), company.cik,
                                                 before=before)
            candidates = [e for e in candidates if force or e.accession_number not in existing]
            if not candidates:
                result.skipped[company.cik] = "no past 10-K/10-Q filings found"
                continue

            seeded: list[str] = []
            # Oldest first, so an interrupted run leaves a coherent history
            # rather than a newest-only one with gaps behind it.
            for event in reversed(candidates[:depth]):
                html = fetch_html(event)
                if not html.strip():
                    raise ValueError(f"Empty filing HTML for {event.accession_number}")
                narrative = screen_filing_narrative(
                    chunk_blocks(parse_filing_html(html)),
                    company_cik=event.company_cik, accession_number=event.accession_number,
                    form=event.form, filing_date=event.filing_date, report_date=event.report_date,
                )
                save_narrative(narrative, narrative_dir)
                seeded.append(event.accession_number)
            result.seeded[company.cik] = seeded
        except Exception as exc:
            result.errors[company.cik] = str(exc)
    return result


def companies_without_baselines(watchlist: Watchlist, narrative_dir: Path | str) -> list[str]:
    """Tickers with no baseline, so a caller can say which ones will report
    `unestablished` rather than letting the user discover it in a memo."""
    return [c.ticker or c.cik for c in watchlist.companies
            if not stored_accessions(narrative_dir, c.cik)]

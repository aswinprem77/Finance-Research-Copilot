"""
SEC EDGAR submissions client — Stage 1 (Trigger Agent), PRD v2 Section 5.

Fetches each watchlist company's filing history and normalizes it into
FilingEvent objects, filtered to the form types this project actually
processes (10-K/10-Q/8-K — see PRD Section 6 "Data Sources").

The automatic worker uses SecXbrlClient.get_submissions to share rate limiting
with companyfacts and HTML requests. This module retains a standalone helper
and normalizes SEC's filings.recent parallel-array structure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import requests

SUBMISSIONS_BASE_URL = "https://data.sec.gov/submissions"

# Per PRD Section 6: 10-K, 10-Q, 8-K are the filing types this project processes.
# A CIK's submissions history includes many other form types (ownership forms,
# proxy statements, etc.) that aren't in scope -- filtered out here, not downstream,
# so nothing downstream has to re-implement this decision.
RELEVANT_FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"}


@dataclass
class FilingEvent:
    company_cik: str
    company_name: str
    accession_number: str
    form: str
    filing_date: date
    report_date: Optional[date]
    primary_document: str


def fetch_submissions(cik: str, user_agent: str) -> dict[str, Any]:
    """
    Fetch raw submissions JSON for a CIK. Same SEC fair-access requirement as
    xbrl_client.py: a descriptive User-Agent (e.g. "Your Name you@example.com")
    or the request gets rejected/rate-limited.
    """
    cik10 = str(int(cik)).zfill(10)
    url = f"{SUBMISSIONS_BASE_URL}/CIK{cik10}.json"
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_recent_filings(raw: dict[str, Any], cik: str) -> list[FilingEvent]:
    """
    Normalizes the submissions API's parallel-array `filings.recent` block into
    a list of FilingEvent, filtered to RELEVANT_FORMS. Malformed/incomplete
    entries are skipped rather than crashing the whole parse -- same defensive
    pattern as xbrl_parser.py's parse_company_facts().

    Only covers `filings.recent` (the last ~1000 filings, or one year, per SEC's
    docs) -- older filings live in a separate paginated `filings.files` block
    this doesn't fetch. Fine for a Trigger agent (which only cares about NEW
    filings going forward), not fine for a full filing-history backfill.
    """
    company_name = raw.get("name") or raw.get("entityName") or ""
    recent = raw.get("filings", {}).get("recent", {})

    accession_numbers = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_documents = recent.get("primaryDocument", [])

    events: list[FilingEvent] = []
    for i in range(len(accession_numbers)):
        try:
            form = forms[i]
            if form not in RELEVANT_FORMS:
                continue
            report_date_str = report_dates[i] if i < len(report_dates) else ""
            events.append(
                FilingEvent(
                    company_cik=cik,
                    company_name=company_name,
                    accession_number=accession_numbers[i],
                    form=form,
                    filing_date=date.fromisoformat(filing_dates[i]),
                    report_date=date.fromisoformat(report_date_str) if report_date_str else None,
                    primary_document=primary_documents[i] if i < len(primary_documents) else "",
                )
            )
        except (IndexError, ValueError, KeyError):
            continue
    return events

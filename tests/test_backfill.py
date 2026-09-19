"""
Tests for narrative baseline seeding.

The point of the module is one behaviour: after backfill, the next filing is
COMPARED instead of being reported as unestablished. test_backfill_makes_the_
next_filing_comparable is that test; the rest guard the ways seeding could do
damage - writing memos for filings it did not analyse at the time, or marking
accessions complete so a real filing never gets one.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.judgment.narrative_store import load_narrative, stored_accessions
from src.pipeline.backfill import (
    backfill_narrative_baselines,
    companies_without_baselines,
    periodic_filings_before,
)
from src.pipeline.runner import process_filing, run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.trigger.edgar_client import FilingEvent
from src.trigger.state_store import load_seen_accessions

FIXTURES = Path(__file__).parent / "fixtures"
CIK = "0000320193"
OLD = "0000320193-24-000010"
NEW = "0000320193-24-000020"

WATCHLIST = Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", CIK)])


def _submissions(*rows) -> dict:
    """rows: (accession, form, filing_date, report_date)"""
    return {"name": "SYNTHETIC TEST CO", "filings": {"recent": {
        "accessionNumber": [r[0] for r in rows],
        "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "reportDate": [r[3] for r in rows],
        "primaryDocument": ["test.htm"] * len(rows),
    }}}


TWO_FILINGS = _submissions(
    (NEW, "10-Q", "2024-07-20", "2024-06-30"),
    (OLD, "10-Q", "2024-04-20", "2024-03-31"),
)


def submissions(cik):
    return TWO_FILINGS


def html(filing):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text())


# --- selecting what to seed --------------------------------------------------

def test_newest_periodic_filings_first():
    events = periodic_filings_before(TWO_FILINGS, CIK)
    assert [e.accession_number for e in events] == [NEW, OLD]


def test_amendments_are_not_used_as_baselines():
    # An amendment restates a filing already covered; seeding from it would
    # compare the next filing against a revision, not against what was said.
    raw = _submissions((NEW, "10-Q/A", "2024-07-20", "2024-06-30"),
                       (OLD, "10-Q", "2024-04-20", "2024-03-31"))
    assert [e.accession_number for e in periodic_filings_before(raw, CIK)] == [OLD]


def test_8k_filings_are_not_used_as_baselines():
    raw = _submissions((NEW, "8-K", "2024-07-20", "2024-06-30"))
    assert periodic_filings_before(raw, CIK) == []


def test_before_excludes_filings_from_that_day_onward():
    events = periodic_filings_before(TWO_FILINGS, CIK, before=date(2024, 7, 20))
    assert [e.accession_number for e in events] == [OLD]


# --- seeding -----------------------------------------------------------------

def test_seeds_the_most_recent_filing_by_default(tmp_path):
    result = backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path)
    assert result.seeded == {CIK: [NEW]}
    assert result.errors == {}
    assert stored_accessions(tmp_path, CIK) == {NEW}


def test_depth_seeds_oldest_first(tmp_path):
    result = backfill_narrative_baselines(WATCHLIST, submissions, html,
                                          narrative_dir=tmp_path, depth=2)
    # Oldest first, so an interrupted run leaves a coherent history.
    assert result.seeded == {CIK: [OLD, NEW]}
    assert stored_accessions(tmp_path, CIK) == {OLD, NEW}


def test_seeded_baseline_carries_screened_passages(tmp_path):
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path)
    stored = load_narrative(tmp_path, CIK, NEW)
    assert stored.passages
    assert stored.form == "10-Q"
    assert stored.filing_date == date(2024, 7, 20)


def test_a_company_with_baselines_is_skipped(tmp_path):
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path)

    def refuse(filing):
        raise AssertionError("a company with baselines must not be refetched")

    result = backfill_narrative_baselines(WATCHLIST, submissions, refuse, narrative_dir=tmp_path)
    assert result.seeded == {}
    assert "already stored" in result.skipped[CIK]


def test_force_reseeds(tmp_path):
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path)
    result = backfill_narrative_baselines(WATCHLIST, submissions, html,
                                          narrative_dir=tmp_path, force=True)
    assert result.seeded == {CIK: [NEW]}


def test_a_company_with_no_periodic_filings_is_reported(tmp_path):
    result = backfill_narrative_baselines(
        Watchlist("t", [WatchlistCompany("X", "X", "0000000001")]),
        lambda cik: _submissions(("0000000001-24-000001", "8-K", "2024-07-20", "")),
        html, narrative_dir=tmp_path)
    assert result.skipped["0000000001"] == "no past 10-K/10-Q filings found"


def test_one_company_failing_does_not_stop_the_others(tmp_path):
    watchlist = Watchlist("t", [WatchlistCompany("BROKEN", "BRK", "0000000009"),
                                WatchlistCompany("SYNTHETIC TEST CO", "TEST", CIK)])

    def flaky(filing):
        if filing.company_cik == "0000000009":
            raise RuntimeError("SEC timeout")
        return html(filing)

    def subs(cik):
        return _submissions((f"{cik}-24-000001", "10-Q", "2024-07-20", "2024-06-30"))

    result = backfill_narrative_baselines(watchlist, subs, flaky, narrative_dir=tmp_path)
    assert "SEC timeout" in result.errors["0000000009"]
    assert result.seeded[CIK]


def test_empty_html_is_an_error_not_an_empty_baseline(tmp_path):
    # An empty baseline would make the next filing look entirely new.
    result = backfill_narrative_baselines(WATCHLIST, submissions, lambda f: "   ",
                                          narrative_dir=tmp_path)
    assert "Empty filing HTML" in result.errors[CIK]
    assert stored_accessions(tmp_path, CIK) == set()


def test_depth_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="depth must be at least 1"):
        backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path, depth=0)


# --- what seeding must not do ------------------------------------------------

def test_seeding_writes_no_memos(tmp_path):
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path / "narrative")
    # A backfilled filing was public before this install existed; a memo would
    # imply the system analysed it at the time.
    assert list(tmp_path.glob("**/*.md")) == []


def test_seeding_does_not_mark_anything_completed(tmp_path):
    state = tmp_path / "completed.json"
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path / "narrative")
    assert load_seen_accessions(state) == set()

    # And the poller still produces memos for seeded filings: seeding must
    # never be the reason a filing silently goes unreported.
    result = run_poll_cycle(WATCHLIST, submissions, facts, html, state_path=state,
                            output_dir=tmp_path / "memos", data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path / "narrative")
    assert sorted(p.name for p in result.completed) == [f"{OLD}.md", f"{NEW}.md"]
    assert load_seen_accessions(state) == {OLD, NEW}


# --- the behaviour this module exists for ------------------------------------

def test_backfill_makes_the_next_filing_comparable(tmp_path):
    narrative_dir = tmp_path / "narrative"

    def later_event():
        return FilingEvent(CIK, "SYNTHETIC TEST CO", "0000320193-24-000031", "8-K",
                           date(2024, 10, 20), date(2024, 9, 30), "test.htm")

    def no_facts(cik):
        raise AssertionError("8-K should not fetch periodic facts")

    # Without a baseline, every screened passage is unestablished.
    cold = process_filing(later_event(), no_facts, html, data_provenance_note="SYNTHETIC",
                          narrative_dir=narrative_dir)
    assert all(f.rule_id.startswith("UNESTABLISHED_")
               for f in cold.memo.flags if f.rule_id.endswith("_LANGUAGE"))
    assert cold.prior_narrative_accession is None

    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=narrative_dir)

    warm = process_filing(later_event(), no_facts, html, data_provenance_note="SYNTHETIC",
                          narrative_dir=narrative_dir)
    language = [f for f in warm.memo.flags if f.rule_id.endswith("_LANGUAGE")]
    assert language
    assert not any(f.rule_id.startswith("UNESTABLISHED_") for f in language)
    assert warm.prior_narrative_accession == NEW


def test_missing_baselines_are_reportable(tmp_path):
    assert companies_without_baselines(WATCHLIST, tmp_path) == ["TEST"]
    backfill_narrative_baselines(WATCHLIST, submissions, html, narrative_dir=tmp_path)
    assert companies_without_baselines(WATCHLIST, tmp_path) == []

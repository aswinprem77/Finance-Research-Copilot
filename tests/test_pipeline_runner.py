from datetime import date
import json
from pathlib import Path

from src.pipeline.runner import process_filing, run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.trigger.edgar_client import FilingEvent
from src.trigger.state_store import load_seen_accessions

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"


def event(form="10-Q"):
    return FilingEvent("0000320193", "SYNTHETIC TEST CO", ACCESSION, form, date(2024, 7, 20), date(2024, 6, 30), "test.htm")


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text())


def html(filing):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


def submissions(cik):
    return {"filings": {"recent": {"accessionNumber": [ACCESSION], "form": ["10-Q"],
        "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"]}}}


def test_end_to_end_filing_scopes_metrics_and_exposes_gaps():
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC")
    assert all(row.period_label == "Q2 FY2024" for row in result.memo.metric_rows)
    assert any(row.concept == "total_debt" and row.provenance == "html_table_fallback" for row in result.memo.metric_rows)
    assert result.coverage_pct == 40
    assert [c.value for c in result.missing_concepts] == ["operating_income"]
    assert "SYNTHETIC" in result.memo.to_markdown()
    assert "Images/charts" in result.memo.to_markdown()


def test_8k_does_not_relabel_periodic_companyfacts():
    def fail(cik):
        raise AssertionError("8-K should not fetch periodic facts")
    result = process_filing(event("8-K"), fail, html, data_provenance_note="SYNTHETIC")
    assert result.memo.metric_rows == []
    assert result.memo.flags


def test_retry_failed_filing_then_acknowledge_and_skip_completed(tmp_path):
    watchlist = Watchlist("Test", [WatchlistCompany("Test", "TEST", "0000320193")])
    options = dict(state_path=tmp_path / "state.json", output_dir=tmp_path / "memos", data_provenance_note="SYNTHETIC")
    def fail(event):
        raise RuntimeError("Temporary HTML failure")
    failed = run_poll_cycle(watchlist, submissions, facts, fail, **options)
    assert ACCESSION in failed.errors
    assert not load_seen_accessions(options["state_path"])
    done = run_poll_cycle(watchlist, submissions, facts, html, **options)
    assert len(done.completed) == 1
    assert "Key Metric Changes" in done.completed[0].read_text(encoding="utf-8")
    assert load_seen_accessions(options["state_path"]) == {ACCESSION}
    repeated = run_poll_cycle(watchlist, submissions, facts, fail, **options)
    assert not repeated.completed and not repeated.errors


def test_failed_memo_write_does_not_acknowledge(tmp_path, monkeypatch):
    watchlist = Watchlist("Test", [WatchlistCompany("Test", "TEST", "0000320193")])
    def fail(*args):
        raise OSError("Disk full")
    monkeypatch.setattr("src.pipeline.runner.write_memo", fail)
    result = run_poll_cycle(watchlist, submissions, facts, html, state_path=tmp_path / "state.json",
        output_dir=tmp_path / "memos", data_provenance_note="Test")
    assert ACCESSION in result.errors
    assert not load_seen_accessions(tmp_path / "state.json")


def test_poll_continues_after_one_company_fetch_fails(tmp_path):
    watchlist = Watchlist("Test", [WatchlistCompany("Bad", "BAD", "1"), WatchlistCompany("Test", "TEST", "0000320193")])
    def fetch(cik):
        if cik == "1":
            raise RuntimeError("Unavailable")
        return submissions(cik)
    result = run_poll_cycle(watchlist, fetch, facts, html, state_path=tmp_path / "state.json",
        output_dir=tmp_path / "memos", data_provenance_note="Test")
    assert "1" in result.errors
    assert len(result.completed) == 1

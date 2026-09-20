from datetime import date
import json
import tempfile
from pathlib import Path

from src.judgment.narrative import screen_filing_narrative
from src.judgment.narrative_store import load_narrative, save_narrative
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
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


# --- prior-filing narrative comparison ---------------------------------------

PRIOR_ACCESSION = "0000320193-24-000010"


def _seed_prior(narrative_dir, source_html: str, accession=PRIOR_ACCESSION):
    """
    Store a previous 10-Q as the baseline.

    The baseline must be the same form class as the filing under test: a 10-Q
    is never compared against an 8-K, so these tests seed a periodic filing
    rather than reusing the fixture's 8-K path.
    """
    narrative = screen_filing_narrative(
        chunk_blocks(parse_filing_html(source_html)),
        company_cik="0000320193", accession_number=accession, form="10-Q",
        filing_date=date(2024, 4, 20), report_date=date(2024, 3, 31))
    save_narrative(narrative, narrative_dir)
    return narrative


def test_first_filing_cannot_claim_new_language(tmp_path):
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path)
    rule_ids = {f.rule_id for f in result.memo.flags}
    assert any(r.startswith("UNESTABLISHED_") for r in rule_ids)
    assert not any(r.startswith("NEW_") for r in rule_ids)
    assert "no prior filing is on record" in result.memo.to_markdown()
    assert result.prior_narrative_accession is None


def test_repeat_filing_marks_unchanged_language_routine(tmp_path):
    _seed_prior(tmp_path, html(None))

    # The prior 10-Q said the same thing, so nothing here is notable any more.
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path)
    language = [f for f in result.memo.flags if f.rule_id.endswith("_LANGUAGE")]
    assert language
    assert all(f.rule_id.startswith("UNCHANGED_") for f in language)
    assert all(f.severity == "routine" for f in language)
    assert result.prior_narrative_accession == PRIOR_ACCESSION


def test_language_appended_to_an_existing_section_is_revised_and_cites_the_edit(tmp_path):
    _seed_prior(tmp_path, html(None))

    def amended_html(filing):
        # Appended inside the existing risk-factors prose, so it lands in the
        # same chunk: a revision of that passage, not a separate one.
        return html(filing).replace(
            "</body>",
            "<p>On September 3, 2024 the Company received a subpoena from state regulators "
            "concerning its export control compliance program.</p></body>",
        )

    second = process_filing(event(), facts, amended_html, data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path)
    revised = [f for f in second.memo.flags if f.rule_id.startswith("REVISED_")]
    assert revised
    assert all(f.severity == "notable" for f in revised)
    assert any("subpoena" in f.detail for f in revised), "the flag must cite the added text"


def test_language_under_a_new_heading_is_flagged_new(tmp_path):
    _seed_prior(tmp_path, html(None))

    def amended_html(filing):
        return html(filing).replace(
            "</body>",
            "<h2>Item 1. Legal Proceedings</h2>"
            "<p>A class action complaint was served by a group of former distributors seeking "
            "unspecified damages relating to our distribution agreements.</p></body>",
        )

    second = process_filing(event(), facts, amended_html, data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path)
    new_flags = [f for f in second.memo.flags if f.rule_id.startswith("NEW_")]
    assert new_flags
    assert all(f.severity == "notable" for f in new_flags)
    assert any("class action" in f.detail for f in new_flags)


def test_narrative_is_persisted_only_after_the_memo_is_written(tmp_path):
    state = tmp_path / "completed.json"
    output = tmp_path / "memos"
    narrative_dir = tmp_path / "narrative"

    def failing_html(filing):
        raise RuntimeError("fetch failed")

    result = run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                            submissions, facts, failing_html, state_path=state, output_dir=output,
                            data_provenance_note="SYNTHETIC", narrative_dir=narrative_dir)
    assert result.errors
    # No memo, so no baseline either - otherwise the next filing would compare
    # against a filing that never produced output.
    assert not narrative_dir.exists() or not any(narrative_dir.rglob("*.json"))


def test_successful_poll_cycle_stores_the_baseline(tmp_path):
    state = tmp_path / "completed.json"
    narrative_dir = tmp_path / "narrative"
    run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                   submissions, facts, html, state_path=state, output_dir=tmp_path / "memos",
                   data_provenance_note="SYNTHETIC", narrative_dir=narrative_dir)
    stored = load_narrative(narrative_dir, "0000320193", ACCESSION)
    assert stored is not None and stored.passages


def test_without_a_narrative_dir_nothing_is_compared_or_written(tmp_path):
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC")
    assert result.narrative is not None  # screened, so a caller can persist it
    assert result.prior_narrative_accession is None
    assert not any(tmp_path.rglob("*.json"))


# --- peer comparison ---------------------------------------------------------

PEERS = [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193"),
         WatchlistCompany("PEER CO", "PEER", "0000789019")]


def test_peer_comparison_is_absent_unless_peers_are_supplied():
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC")
    assert result.peer_comparison is None
    assert "## Peer Comparison" not in result.memo.to_markdown()


def test_peer_comparison_reaches_the_memo():
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC",
                            peers=PEERS, fetch_peer_facts=facts)
    assert result.peer_comparison is not None
    markdown = result.memo.to_markdown()
    assert "## Peer Comparison" in markdown
    assert "aligned on period end date, not fiscal label" in markdown
    # The subject is never listed as its own peer.
    assert all(row.company_cik != "0000320193" for row in result.peer_comparison.peers)


def test_a_failing_peer_is_named_in_the_table_and_does_not_fail_the_filing():
    def flaky(cik):
        if cik == "0000789019":
            raise RuntimeError("SEC timeout")
        return facts(cik)

    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC",
                            peers=PEERS, fetch_peer_facts=flaky)
    broken = next(r for r in result.peer_comparison.peers if r.company_cik == "0000789019")
    assert "SEC timeout" in broken.unavailable_reason
    assert "Peers not aligned, and why" in result.memo.to_markdown()


def test_peer_facts_are_fetched_once_per_poll_cycle():
    calls = []

    def counting(cik):
        calls.append(cik)
        return facts(cik)

    run_poll_cycle(Watchlist("t", PEERS), submissions, counting, html,
                   state_path=Path(tempfile.mkdtemp()) / "completed.json",
                   output_dir=Path(tempfile.mkdtemp()), data_provenance_note="SYNTHETIC")
    # Companyfacts documents are large; one fetch per company per cycle.
    assert calls.count("0000789019") <= 1


def test_peer_comparisons_can_be_switched_off():
    result = run_poll_cycle(Watchlist("t", PEERS), submissions, facts, html,
                            state_path=Path(tempfile.mkdtemp()) / "completed.json",
                            output_dir=Path(tempfile.mkdtemp()), data_provenance_note="SYNTHETIC",
                            peer_comparisons=False)
    assert result.completed
    assert "## Peer Comparison" not in result.completed[0].read_text(encoding="utf-8")

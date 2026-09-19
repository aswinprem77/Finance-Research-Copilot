import json
from pathlib import Path

from src.evaluation.prepare import export_review_files, prepare_review_queue
from src.pipeline.watchlist import Watchlist, WatchlistCompany

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"


def submissions(cik):
    return {"name": "SYNTHETIC TEST CO", "filings": {"recent": {
        "accessionNumber": [ACCESSION], "form": ["10-Q"],
        "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"],
    }}}


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text(encoding="utf-8"))


def html(event):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


def test_prepares_unlabeled_review_queue(tmp_path):
    watchlist = Watchlist("Synthetic", [WatchlistCompany("Test Co", "TEST", "0000320193")])
    queue = prepare_review_queue(watchlist, submissions, facts, html, tmp_path, filings_per_company=1)
    assert queue["status"] == "draft_unlabeled"
    assert queue["errors"] == {}
    assert len(queue["filings"]) == 1
    filing = queue["filings"][0]
    assert filing["filing"]["accession_number"] == ACCESSION
    assert filing["numeric_review"]
    assert all(item["verified"] is None and item["verified_value"] is None for item in filing["numeric_review"])
    assert len(filing["retrieval_review"]) == 5
    assert all(result["relevant"] is None for query in filing["retrieval_review"] for result in query["results"])
    assert (tmp_path / "review_queue.json").is_file()
    assert (tmp_path / "memos" / f"{ACCESSION}.md").is_file()
    assert (tmp_path / "numeric_review.csv").is_file()
    assert (tmp_path / "retrieval_review.csv").is_file()
    assert (tmp_path / "filing_summary.csv").is_file()
    assert len(filing["numeric_review"]) == 4


def test_export_deduplicates_existing_comparison_rows(tmp_path):
    watchlist = Watchlist("Synthetic", [WatchlistCompany("Test Co", "TEST", "0000320193")])
    queue = prepare_review_queue(watchlist, submissions, facts, html, tmp_path, filings_per_company=1)
    duplicate = dict(queue["filings"][0]["numeric_review"][0])
    queue["filings"][0]["numeric_review"].append(duplicate)
    counts = export_review_files(queue, tmp_path)
    assert counts["numeric_checks"] == 4


def test_company_failure_does_not_block_remaining_review_cases(tmp_path):
    watchlist = Watchlist("Synthetic", [
        WatchlistCompany("Bad", "BAD", "1"),
        WatchlistCompany("Test Co", "TEST", "0000320193"),
    ])
    def fetch_submissions(cik):
        if cik == "1":
            raise RuntimeError("temporary SEC failure")
        return submissions(cik)
    queue = prepare_review_queue(watchlist, fetch_submissions, facts, html, tmp_path, filings_per_company=1)
    assert queue["errors"]["1"] == "temporary SEC failure"
    assert len(queue["filings"]) == 1

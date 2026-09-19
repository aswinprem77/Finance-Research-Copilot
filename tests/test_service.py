"""Tests for the run records, the read API and PDF export."""
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.analyst.peers import build_peer_comparison
from src.pipeline.runner import process_filing, run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.schema.financial_schema import (
    CompanyFinancials,
    FactSource,
    FinancialConcept,
    FinancialFact,
    FiscalPeriod,
)
from src.service.api import create_app
from src.service.pdf import memo_pdf_bytes
from src.service.records import (
    RunRecord,
    build_run_record,
    list_run_records,
    load_run_record,
    record_path,
    save_run_record,
)
from src.trigger.edgar_client import FilingEvent

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"


def event(form="10-Q"):
    return FilingEvent("0000320193", "SYNTHETIC TEST CO", ACCESSION, form,
                       date(2024, 7, 20), date(2024, 6, 30), "test.htm")


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text())


def html(filing):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


def submissions(cik):
    return {"filings": {"recent": {"accessionNumber": [ACCESSION], "form": ["10-Q"],
            "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"]}}}


@pytest.fixture
def populated(tmp_path):
    """A data directory in the shape the pipeline leaves behind."""
    run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                   submissions, facts, html,
                   state_path=tmp_path / "completed.json", output_dir=tmp_path / "memos",
                   data_provenance_note="SYNTHETIC", records_dir=tmp_path / "records")
    return tmp_path


@pytest.fixture
def client(populated):
    return TestClient(create_app(populated))


# --- run records -------------------------------------------------------------

def test_poll_cycle_writes_a_record_per_filing(populated):
    records = list_run_records(populated / "records")
    assert [r.accession_number for r in records] == [ACCESSION]
    assert records[0].memo_filename == f"{ACCESSION}.md"
    assert records[0].coverage_pct == 40


def test_record_round_trips(tmp_path):
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC")
    record = build_run_record(result, memo_filename=f"{ACCESSION}.md")
    save_run_record(record, tmp_path)
    assert load_run_record(tmp_path, ACCESSION) == record


def test_flag_counts_split_notable_from_routine(tmp_path):
    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC",
                            narrative_dir=tmp_path)
    record = build_run_record(result, memo_filename="x.md")
    assert record.notable_flag_count + record.routine_flag_count == len(record.flags)
    assert record.notable_flag_count > 0  # first filing: unestablished language


def test_no_record_is_written_without_a_records_dir(tmp_path):
    run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                   submissions, facts, html, state_path=tmp_path / "completed.json",
                   output_dir=tmp_path / "memos", data_provenance_note="SYNTHETIC")
    assert not (tmp_path / "records").exists()


def test_a_failed_filing_leaves_no_record(tmp_path):
    def failing_html(filing):
        raise RuntimeError("fetch failed")

    result = run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                            submissions, facts, failing_html, state_path=tmp_path / "completed.json",
                            output_dir=tmp_path / "memos", data_provenance_note="SYNTHETIC",
                            records_dir=tmp_path / "records")
    assert result.errors
    assert list_run_records(tmp_path / "records") == []


def test_malformed_accession_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid SEC accession number"):
        record_path(tmp_path, "../escape")


def test_unreadable_records_are_skipped_not_fatal(populated):
    (populated / "records" / "0000000000-00-000000.json").write_text("{ not json", encoding="utf-8")
    future = json.loads((populated / "records" / f"{ACCESSION}.json").read_text(encoding="utf-8"))
    future["version"] = "v99"
    (populated / "records" / "0000000000-00-000001.json").write_text(json.dumps(future), encoding="utf-8")
    # One good record survives both a corrupt file and a future-version one.
    assert [r.accession_number for r in list_run_records(populated / "records")] == [ACCESSION]


def test_listing_is_newest_filing_first(tmp_path):
    base = build_run_record(process_filing(event(), facts, html, data_provenance_note="S"),
                            memo_filename="a.md")
    for accession, filing_date in (("0000320193-24-000010", date(2024, 1, 5)),
                                   ("0000320193-24-000030", date(2024, 11, 5))):
        record = RunRecord.from_dict({**base.to_dict(), "accession_number": accession,
                                      "filing_date": filing_date.isoformat()})
        save_run_record(record, tmp_path)
    save_run_record(base, tmp_path)
    assert [r.accession_number for r in list_run_records(tmp_path)] == [
        "0000320193-24-000030", ACCESSION, "0000320193-24-000010"]


# --- API ---------------------------------------------------------------------

def test_health_reports_what_it_can_see(client, populated):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["processed_filings"] == 1
    assert body["records_available"] is True


def test_health_works_on_an_empty_data_dir(tmp_path):
    body = TestClient(create_app(tmp_path)).get("/health").json()
    assert body["status"] == "ok"
    assert body["processed_filings"] == 0
    assert body["records_available"] is False


def test_filings_listing_returns_summaries(client):
    body = client.get("/filings").json()
    assert body["count"] == 1
    summary = body["filings"][0]
    assert summary["accession_number"] == ACCESSION
    assert summary["form"] == "10-Q"
    # The listing is a summary: the full memo content is not in it.
    assert "metric_rows" not in summary


def test_filings_can_be_filtered(client):
    assert client.get("/filings", params={"cik": "0000320193"}).json()["count"] == 1
    assert client.get("/filings", params={"cik": "0000000000"}).json()["count"] == 0
    assert client.get("/filings", params={"form": "10-k"}).json()["count"] == 0
    assert client.get("/filings", params={"form": "10-q"}).json()["count"] == 1


def test_limit_is_bounded(client):
    assert client.get("/filings", params={"limit": 0}).status_code == 422
    assert client.get("/filings", params={"limit": 501}).status_code == 422


def test_single_filing_returns_the_whole_record(client):
    body = client.get(f"/filings/{ACCESSION}").json()
    assert body["accession_number"] == ACCESSION
    assert body["metric_rows"]
    assert body["data_provenance_note"].startswith("SYNTHETIC")


def test_unknown_filing_is_404(client):
    assert client.get("/filings/0000000000-00-000000").status_code == 404


def test_malformed_accession_is_400_not_500(client):
    assert client.get("/filings/not-an-accession").status_code == 400


def test_flags_endpoint_filters_by_severity(client):
    every = client.get(f"/filings/{ACCESSION}/flags").json()
    routine = client.get(f"/filings/{ACCESSION}/flags", params={"severity": "routine"}).json()
    assert every["count"] >= routine["count"]
    assert all(f["severity"] == "routine" for f in routine["flags"])


def test_memo_markdown_is_served_verbatim(client, populated):
    served = client.get(f"/filings/{ACCESSION}/memo.md")
    assert served.status_code == 200
    assert served.text == (populated / "memos" / f"{ACCESSION}.md").read_text(encoding="utf-8")


def test_missing_memo_file_is_404(client, populated):
    (populated / "memos" / f"{ACCESSION}.md").unlink()
    assert client.get(f"/filings/{ACCESSION}/memo.md").status_code == 404


def test_peers_endpoint_404s_when_none_was_produced(client):
    assert client.get(f"/filings/{ACCESSION}/peers").status_code == 404


def test_watchlist_is_served(client):
    body = client.get("/watchlist").json()
    assert body["sector"]
    assert all({"name", "ticker", "cik"} <= set(c) for c in body["companies"])


def test_poll_refuses_rather_than_corrupting_state(client):
    # An API worker cannot guarantee it is the single owner of the state file.
    response = client.post("/poll")
    assert response.status_code == 501
    assert "durable queue" in response.json()["detail"]


# --- PDF ---------------------------------------------------------------------

def _peer_record():
    def company(cik, ticker, revenue, net_income):
        return CompanyFinancials(company_cik=cik, company_name=ticker, company_ticker=ticker, facts=[
            FinancialFact(company_cik=cik, concept=c, value=v, fiscal_year=2026,
                          fiscal_period=FiscalPeriod.Q2, period_end_date=date(2026, 6, 30),
                          source=FactSource.XBRL)
            for c, v in ((FinancialConcept.REVENUE, revenue), (FinancialConcept.NET_INCOME, net_income))])

    result = process_filing(event(), facts, html, data_provenance_note="SYNTHETIC")
    result.memo.peer_comparison = build_peer_comparison(
        company("0000000001", "SUBJ", 100.0, 10.0), [company("0000000002", "LOSS", 200.0, -50.0)],
        fiscal_year=2026, fiscal_period=FiscalPeriod.Q2, period_end_date=date(2026, 6, 30))
    return build_run_record(result, memo_filename="x.md")


def test_pdf_renders_a_real_document(populated):
    record = load_run_record(populated / "records", ACCESSION)
    pdf = memo_pdf_bytes(record)
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1500


def test_pdf_renders_a_peer_table(populated):
    assert memo_pdf_bytes(_peer_record()).startswith(b"%PDF-")


def test_pdf_survives_markup_characters_in_filing_prose():
    # reportlab paragraphs are mini-HTML; unescaped angle brackets would raise.
    record = _peer_record()
    record.flags.append({"rule_id": "T", "rule_description": "a < b & c > d",
                         "severity": "notable", "citation": "<Item 1A>",
                         "detail": "Revenue & margin <fell> sharply"})
    assert memo_pdf_bytes(record).startswith(b"%PDF-")


def test_pdf_endpoint_serves_an_attachment(client):
    response = client.get(f"/filings/{ACCESSION}/memo.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert ACCESSION in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")

import json
from pathlib import Path

from src.trigger.edgar_client import RELEVANT_FORMS, parse_recent_filings

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_submissions.json"


def _raw():
    return json.loads(FIXTURE_PATH.read_text())


def test_filters_to_relevant_forms_only():
    # Fixture has 4 raw entries: 10-Q, 8-K, 4 (insider form -- irrelevant), 10-K.
    events = parse_recent_filings(_raw(), cik="0000320193")
    assert len(events) == 3
    assert all(e.form in RELEVANT_FORMS for e in events)
    assert not any(e.form == "4" for e in events)


def test_parses_dates_correctly():
    events = parse_recent_filings(_raw(), cik="0000320193")
    tenq = next(e for e in events if e.form == "10-Q")
    assert tenq.filing_date.isoformat() == "2024-10-20"
    assert tenq.report_date.isoformat() == "2024-09-30"


def test_handles_empty_report_date():
    # The 8-K entry in the fixture has an empty reportDate string -- real SEC data
    # does this for some filing types. Must become None, not crash on date.fromisoformat("").
    events = parse_recent_filings(_raw(), cik="0000320193")
    eightk = next(e for e in events if e.form == "8-K")
    assert eightk.report_date is None


def test_carries_company_cik_and_name():
    events = parse_recent_filings(_raw(), cik="0000320193")
    assert all(e.company_cik == "0000320193" for e in events)
    assert all(e.company_name == "SYNTHETIC TEST CO" for e in events)


def test_empty_recent_filings_returns_empty_list():
    raw = {"cik": "1", "name": "Empty Co", "filings": {"recent": {}}}
    assert parse_recent_filings(raw, cik="1") == []

"""
Tests for delivery.

The behaviours that matter most here are the refusals: a delivery failure must
not cost a memo or block an acknowledgement, a routine-only filing must not
generate an alert, and a webhook URL must never appear in an error message.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.output.memo import SCOPE_DISCLAIMER
from src.pipeline.runner import run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.service.notify import (
    EmailNotifier,
    NullNotifier,
    SlackWebhookNotifier,
    build_notification,
    build_notifier_from_env,
    deliver,
    deliver_pending,
    filing_url,
    should_notify,
)
from src.service.records import RunRecord, list_run_records, load_run_record, save_run_record
from src.trigger.state_store import load_seen_accessions

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"
WATCHLIST = Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")])


def submissions(cik):
    return {"filings": {"recent": {"accessionNumber": [ACCESSION], "form": ["10-Q"],
            "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"]}}}


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text())


def html(filing):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


def _record(notable=2, routine=0, **kw) -> RunRecord:
    flags = [{"rule_id": f"NEW_LITIGATION_LANGUAGE", "rule_description": "d", "severity": "notable",
              "citation": "Item 1A", "detail": f"A lawsuit number {i} was filed."} for i in range(notable)]
    flags += [{"rule_id": "UNCHANGED_LITIGATION_LANGUAGE", "rule_description": "d",
               "severity": "routine", "citation": "Item 1A", "detail": "Boilerplate."}
              for _ in range(routine)]
    defaults = dict(
        accession_number=ACCESSION, company_cik="0000320193", company_name="SYNTHETIC TEST CO",
        company_ticker="TEST", form="10-Q", filing_date=date(2024, 7, 20),
        report_date=date(2024, 6, 30), processed_at="2024-07-20T00:00:00+00:00",
        coverage_pct=40.0, missing_concepts=["operating_income"], elapsed_seconds=1.0,
        memo_filename=f"{ACCESSION}.md", data_provenance_note="SYNTHETIC", rubric_version="v2",
        flags=flags,
    )
    defaults.update(kw)
    return RunRecord(**defaults)


class Recorder:
    """A notifier that captures instead of sending."""

    name = "recorder"

    def __init__(self, fail: Exception | None = None):
        self.sent = []
        self._fail = fail

    def send(self, notification):
        if self._fail:
            raise self._fail
        self.sent.append(notification)


# --- policy ------------------------------------------------------------------

def test_notable_findings_trigger_an_alert():
    assert should_notify(_record(notable=1))


def test_routine_only_filings_do_not():
    # Alerting on unchanged boilerplate is how a notifier trains its reader to
    # ignore it.
    assert not should_notify(_record(notable=0, routine=4))


def test_threshold_is_configurable():
    assert not should_notify(_record(notable=1), min_notable=2)
    assert should_notify(_record(notable=2), min_notable=2)


# --- message -----------------------------------------------------------------

def test_message_names_the_company_form_and_count():
    note = build_notification(_record(notable=2))
    assert "TEST" in note.subject
    assert "2 notable finding(s)" in note.subject
    assert "10-Q" in note.subject


def test_message_links_the_filing_and_carries_the_disclaimer():
    note = build_notification(_record())
    assert filing_url(_record()) in note.body
    assert "sec.gov/Archives/edgar/data/1045810" not in note.body  # correct CIK, not another
    assert SCOPE_DISCLAIMER in note.body


def test_message_reports_coverage_and_unresolved_concepts():
    body = build_notification(_record()).body
    assert "XBRL coverage 40%" in body
    assert "operating_income" in body


def test_long_flag_lists_are_summarised():
    body = build_notification(_record(notable=9)).body
    assert "and 4 more notable finding(s)" in body


def test_routine_items_are_counted_but_not_listed():
    body = build_notification(_record(notable=1, routine=3)).body
    assert "3 routine item(s)" in body
    assert "Boilerplate." not in body


# --- transports --------------------------------------------------------------

def test_slack_posts_the_payload():
    calls = {}

    def post(url, json=None, timeout=None):
        calls.update(url=url, payload=json)
        return type("R", (), {"status_code": 200})()

    SlackWebhookNotifier("https://hooks.example/abc", post=post).send(build_notification(_record()))
    assert calls["url"] == "https://hooks.example/abc"
    assert "TEST" in calls["payload"]["text"]


def test_slack_error_never_leaks_the_webhook_url():
    # The URL is the credential; an error string tends to end up in logs.
    secret = "https://hooks.example/SUPERSECRET"

    def post(url, json=None, timeout=None):
        return type("R", (), {"status_code": 403})()

    status, error = deliver(_record(), SlackWebhookNotifier(secret, post=post))
    assert status == "failed"
    assert "403" in error
    assert "SUPERSECRET" not in error


def test_empty_webhook_is_rejected():
    with pytest.raises(ValueError, match="webhook URL is empty"):
        SlackWebhookNotifier("")


def test_email_builds_a_plain_message():
    captured = {}
    notifier = EmailNotifier(host="smtp.example", port=587, sender="a@example.com",
                             recipients=["b@example.com", "c@example.com"],
                             transport=lambda m: captured.update(msg=m))
    notifier.send(build_notification(_record()))
    message = captured["msg"]
    assert message["To"] == "b@example.com, c@example.com"
    assert "notable finding(s)" in message["Subject"]
    assert SCOPE_DISCLAIMER in message.get_content()


def test_email_requires_its_settings():
    with pytest.raises(ValueError, match="SMTP_HOST"):
        EmailNotifier(host="", port=587, sender="a@example.com", recipients=["b@example.com"])


# --- selection from environment ----------------------------------------------

def test_slack_wins_when_both_are_configured():
    notifier = build_notifier_from_env({"SLACK_WEBHOOK_URL": "https://hooks.example/x",
                                        "SMTP_HOST": "smtp.example",
                                        "NOTIFY_EMAIL_FROM": "a@x.com", "NOTIFY_EMAIL_TO": "b@x.com"})
    assert notifier.name == "slack"


def test_email_is_used_when_only_smtp_is_configured():
    notifier = build_notifier_from_env({"SMTP_HOST": "smtp.example", "NOTIFY_EMAIL_FROM": "a@x.com",
                                        "NOTIFY_EMAIL_TO": "b@x.com, c@x.com"})
    assert notifier.name == "email"


def test_nothing_configured_yields_a_null_notifier():
    # An unconfigured install must still poll normally.
    assert build_notifier_from_env({}).name == "none"
    assert build_notifier_from_env({"SLACK_WEBHOOK_URL": "   "}).name == "none"


def test_partial_email_config_does_not_half_enable_delivery():
    assert build_notifier_from_env({"SMTP_HOST": "smtp.example"}).name == "none"


# --- deliver() ---------------------------------------------------------------

def test_deliver_reports_sent():
    recorder = Recorder()
    assert deliver(_record(), recorder) == ("sent", None)
    assert len(recorder.sent) == 1


def test_deliver_skips_without_a_notifier():
    status, reason = deliver(_record(), NullNotifier())
    assert status == "skipped" and "no notifier" in reason


def test_deliver_skips_routine_only():
    status, reason = deliver(_record(notable=0, routine=2), Recorder())
    assert status == "skipped" and "no notable findings" in reason


def test_deliver_never_raises():
    status, error = deliver(_record(), Recorder(fail=RuntimeError("network down")))
    assert status == "failed"
    assert "network down" in error


# --- pipeline integration ----------------------------------------------------

def _poll(tmp_path, notifier, **kw):
    return run_poll_cycle(WATCHLIST, submissions, facts, html,
                          state_path=tmp_path / "completed.json", output_dir=tmp_path / "memos",
                          data_provenance_note="SYNTHETIC", records_dir=tmp_path / "records",
                          narrative_dir=tmp_path / "narrative", notifier=notifier, **kw)


def test_poll_sends_and_records_delivery(tmp_path):
    recorder = Recorder()
    result = _poll(tmp_path, recorder)
    assert result.completed and not result.delivery_failures
    assert [n.accession_number for n in recorder.sent] == [ACCESSION]
    record = load_run_record(tmp_path / "records", ACCESSION)
    assert record.delivery_status == "sent"
    assert record.delivered_at and record.delivery_error is None


def test_a_delivery_failure_costs_neither_the_memo_nor_the_acknowledgement(tmp_path):
    result = _poll(tmp_path, Recorder(fail=RuntimeError("slack is down")))
    assert result.completed, "the memo must still be written"
    assert not result.errors, "a delivery failure is not a filing failure"
    assert "slack is down" in result.delivery_failures[ACCESSION]
    # Acknowledged, so the next poll does not reprocess a whole filing to
    # retry one HTTP POST.
    assert load_seen_accessions(tmp_path / "completed.json") == {ACCESSION}
    assert load_run_record(tmp_path / "records", ACCESSION).delivery_status == "failed"


def test_delivery_status_is_pending_without_a_notifier(tmp_path):
    _poll(tmp_path, NullNotifier())
    assert load_run_record(tmp_path / "records", ACCESSION).delivery_status == "skipped"


# --- retry from records ------------------------------------------------------

def test_pending_records_are_retried_without_reprocessing(tmp_path):
    records = tmp_path / "records"
    save_run_record(_record(), records)
    recorder = Recorder()
    outcome = deliver_pending(records, recorder)
    assert outcome["sent"] == [ACCESSION]
    assert load_run_record(records, ACCESSION).delivery_status == "sent"


def test_already_sent_records_are_not_resent(tmp_path):
    records = tmp_path / "records"
    save_run_record(_record(delivery_status="sent", delivered_at="2024-07-20T00:00:00+00:00"), records)
    recorder = Recorder()
    assert deliver_pending(records, recorder)["sent"] == []
    assert recorder.sent == []


def test_failed_records_are_retried_and_can_be_excluded(tmp_path):
    records = tmp_path / "records"
    save_run_record(_record(delivery_status="failed", delivery_error="boom"), records)
    assert deliver_pending(records, Recorder(), include_failed=False)["sent"] == []
    assert deliver_pending(records, Recorder())["sent"] == [ACCESSION]


def test_retry_records_a_second_failure(tmp_path):
    records = tmp_path / "records"
    save_run_record(_record(), records)
    outcome = deliver_pending(records, Recorder(fail=RuntimeError("still down")))
    assert "still down" in outcome["failed"][ACCESSION]
    assert load_run_record(records, ACCESSION).delivery_status == "failed"


def test_delivery_status_appears_in_the_listing_summary(tmp_path):
    save_run_record(_record(delivery_status="sent"), tmp_path)
    assert list_run_records(tmp_path)[0].summary()["delivery_status"] == "sent"


def test_alert_headline_uses_the_watchlist_ticker(tmp_path):
    # SEC filing events carry no ticker; it comes from the watchlist so a
    # channel message reads "TEST", not "SYNTHETIC TEST CO".
    recorder = Recorder()
    _poll(tmp_path, recorder)
    assert recorder.sent[0].subject.startswith("TEST:")
    assert load_run_record(tmp_path / "records", ACCESSION).company_ticker == "TEST"

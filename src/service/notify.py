"""
Delivery - PRD v2 Section 5 Stage 5, "optional Slack/email webhook", and the
half of Section 2's use case that nothing has yet answered: "Alert me when
something material changes in a filing from my watchlist."

Until now the pipeline detected, analysed and wrote a memo to a folder, and
nobody was told. An analyst still had to go looking, which is the reactive
behaviour the problem statement opens by complaining about.

Three decisions shape this module:

1. Delivery is BEST EFFORT and never fails a filing. The memo is the durable
   deliverable; a Slack outage must not cost one, and must not stop the
   accession being acknowledged either, or a webhook hiccup would trigger a
   full reprocess - XBRL, HTML, embeddings - to retry one HTTP POST. Instead
   the outcome is written into the run record, and `--notify-pending` retries
   from records alone, with no pipeline work at all.

2. Only NOTABLE findings are delivered. Every filing produces a memo; most
   produce nothing an analyst needs to be interrupted for. A notifier that
   fires on all of them trains its reader to ignore it, which is the same
   noise failure the rubric was built to avoid. Routine flags are counted in
   the message but never trigger it.

3. Secrets stay in the environment. Webhook URLs and SMTP passwords are read
   from env, never stored in a run record, never logged, and never echoed in
   an error message - a failed POST would otherwise print a webhook URL that
   is itself the credential.

Messages carry the scope disclaimer for the same reason every memo does: a
one-line alert is the most likely thing to be read without the document
behind it.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from src.output.memo import SCOPE_DISCLAIMER
from src.service.records import RunRecord, list_run_records, save_run_record

ENV_SLACK_WEBHOOK = "SLACK_WEBHOOK_URL"
ENV_EMAIL_TO = "NOTIFY_EMAIL_TO"
ENV_EMAIL_FROM = "NOTIFY_EMAIL_FROM"
ENV_SMTP_HOST = "SMTP_HOST"
ENV_SMTP_PORT = "SMTP_PORT"
ENV_SMTP_USER = "SMTP_USERNAME"
ENV_SMTP_PASSWORD = "SMTP_PASSWORD"

# How many flags to name before summarising the rest. An alert is a pointer to
# the memo, not a copy of it.
MAX_FLAGS_IN_MESSAGE = 5


def filing_url(record: RunRecord) -> str:
    accession = record.accession_number
    return (f"https://www.sec.gov/Archives/edgar/data/{int(record.company_cik)}/"
            f"{accession.replace('-', '')}/{accession}-index.html")


@dataclass(frozen=True)
class Notification:
    subject: str
    body: str
    accession_number: str

    def to_slack_payload(self) -> dict:
        return {"text": f"*{self.subject}*\n{self.body}"}


def should_notify(record: RunRecord, *, min_notable: int = 1) -> bool:
    """Notable findings only - see decision 2 in the module docstring."""
    return record.notable_flag_count >= min_notable


def build_notification(record: RunRecord) -> Notification:
    who = record.company_ticker or record.company_name
    subject = (f"{who}: {record.notable_flag_count} notable finding(s) in "
               f"{record.form} filed {record.filing_date}")

    notable = [f for f in record.flags if f.get("severity") != "routine"]
    header = [f"{record.company_name} ({record.company_cik}) - {record.form}, "
              f"accession {record.accession_number}"]
    if record.report_date:
        header.append(f"Period ended {record.report_date}")
    coverage = f"XBRL coverage {record.coverage_pct:.0f}%"
    if record.missing_concepts:
        coverage += f"; unresolved: {', '.join(record.missing_concepts)}"
    header.append(coverage)

    findings = []
    for flag in notable[:MAX_FLAGS_IN_MESSAGE]:
        detail = " ".join(str(flag.get("detail", "")).split())[:220]
        findings.append(f"- [{flag.get('rule_id')}] {detail}")
    if len(notable) > MAX_FLAGS_IN_MESSAGE:
        findings.append(f"- ...and {len(notable) - MAX_FLAGS_IN_MESSAGE} more notable finding(s)")
    if record.routine_flag_count:
        findings.append(f"({record.routine_flag_count} routine item(s) repeat prior-filing "
                        "language; not listed.)")

    sections = ["\n".join(header), "\n".join(findings),
                f"Filing: {filing_url(record)}", SCOPE_DISCLAIMER]
    return Notification(subject=subject,
                        body="\n\n".join(s for s in sections if s).strip(),
                        accession_number=record.accession_number)


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None:
        """Deliver, or raise. Callers treat any exception as a delivery failure."""
        ...


class NullNotifier:
    """The default. Delivers nothing and says so, rather than pretending."""

    name = "none"

    def send(self, notification: Notification) -> None:
        return None


class SlackWebhookNotifier:
    """
    Slack incoming webhook.

    `post` is injected so tests exercise this without network. The webhook URL
    is a bearer credential, so it is never included in an exception message.
    """

    name = "slack"

    def __init__(self, webhook_url: str, post: Callable | None = None, timeout: float = 10.0):
        if not webhook_url:
            raise ValueError("Slack webhook URL is empty")
        self._url = webhook_url
        self._timeout = timeout
        self._post = post

    def send(self, notification: Notification) -> None:
        post = self._post
        if post is None:
            import requests
            post = requests.post
        response = post(self._url, json=notification.to_slack_payload(), timeout=self._timeout)
        status = getattr(response, "status_code", 200)
        if status >= 400:
            # Deliberately no URL in the message: it is the credential.
            raise RuntimeError(f"Slack webhook rejected the message with HTTP {status}")


class EmailNotifier:
    """SMTP delivery. `transport` is injected for the same reason `post` is above."""

    name = "email"

    def __init__(self, host: str, port: int, sender: str, recipients: list[str],
                 username: str | None = None, password: str | None = None,
                 use_tls: bool = True, transport: Callable | None = None, timeout: float = 20.0):
        if not host or not sender or not recipients:
            raise ValueError("Email notification needs SMTP_HOST, NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO")
        self._host, self._port = host, port
        self._sender, self._recipients = sender, recipients
        self._username, self._password = username, password
        self._use_tls, self._timeout = use_tls, timeout
        self._transport = transport

    def _build(self, notification: Notification) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = notification.subject
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(notification.body)
        return message

    def send(self, notification: Notification) -> None:
        message = self._build(notification)
        if self._transport is not None:
            self._transport(message)
            return
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


def build_notifier_from_env(env: dict[str, str] | None = None) -> Notifier:
    """
    Slack if a webhook is configured, else email if SMTP is, else nothing.

    Returning NullNotifier rather than raising is deliberate: delivery is
    optional, and an unconfigured install should poll normally rather than
    refuse to start. The CLI reports which notifier is active so "nothing was
    sent" is never a silent surprise.
    """
    env = os.environ if env is None else env
    webhook = (env.get(ENV_SLACK_WEBHOOK) or "").strip()
    if webhook:
        return SlackWebhookNotifier(webhook)

    host = (env.get(ENV_SMTP_HOST) or "").strip()
    recipients = [r.strip() for r in (env.get(ENV_EMAIL_TO) or "").split(",") if r.strip()]
    sender = (env.get(ENV_EMAIL_FROM) or "").strip()
    if host and recipients and sender:
        return EmailNotifier(
            host=host, port=int(env.get(ENV_SMTP_PORT) or 587), sender=sender,
            recipients=recipients, username=env.get(ENV_SMTP_USER) or None,
            password=env.get(ENV_SMTP_PASSWORD) or None,
        )
    return NullNotifier()


def deliver(record: RunRecord, notifier: Notifier, *, min_notable: int = 1) -> tuple[str, str | None]:
    """
    Attempt delivery for one record.

    Returns (status, error). Status is one of `sent`, `skipped` (nothing
    notable) or `failed`. Never raises: a delivery problem is data about that
    filing, not a reason to lose it.
    """
    if isinstance(notifier, NullNotifier):
        return "skipped", "no notifier configured"
    if not should_notify(record, min_notable=min_notable):
        return "skipped", "no notable findings"
    try:
        notifier.send(build_notification(record))
        return "sent", None
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"


def deliver_pending(records_dir: Path | str, notifier: Notifier, *, min_notable: int = 1,
                    include_failed: bool = True) -> dict:
    """
    Retry delivery from stored records alone - no SEC calls, no XBRL, no
    embeddings. This is why a webhook outage does not justify reprocessing a
    filing: everything the alert needs is already on disk.

    Records already `sent` are never re-sent, so this is safe to run repeatedly.
    """
    wanted = {"pending"} | ({"failed"} if include_failed else set())
    outcome = {"sent": [], "skipped": [], "failed": {}}
    for record in list_run_records(records_dir):
        if record.delivery_status not in wanted:
            continue
        status, error = deliver(record, notifier, min_notable=min_notable)
        record.delivery_status, record.delivery_error = status, error
        if status == "sent":
            record.delivered_at = datetime.now(timezone.utc).isoformat()
            outcome["sent"].append(record.accession_number)
        elif status == "failed":
            outcome["failed"][record.accession_number] = error
        else:
            outcome["skipped"].append(record.accession_number)
        save_run_record(record, records_dir)
    return outcome

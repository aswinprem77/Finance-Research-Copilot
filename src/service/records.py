"""
Structured records of processed filings.

Until now a completed filing left two traces: a Markdown memo for a human to
read, and an accession string in completed.json so it is not processed twice.
Neither is queryable. Anything that wants to answer "which filings did we
process, what coverage did they get, how many notable flags" has to re-run the
pipeline or parse prose.

So each run also writes a RunRecord: the same memo, structured. It is what the
API serves, what the PDF renderer draws from, and the row a database table
would hold if the JSON files are outgrown - which is why the shape here is
flat and boring rather than a mirror of the in-memory dataclasses.

Written after the memo and before the accession is acknowledged, matching the
existing rule that nothing is acknowledged before its output is durable. Files
are replaced atomically for the same reason the trigger state is.

RECORD_VERSION is checked on read. A record written by a newer version is
skipped rather than guessed at, because a listing that silently drops fields
is worse than one that says it cannot read them.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.analyst.peers import PeerComparison
from src.output.memo import Memo
from src.schema.financial_schema import FinancialConcept

RECORD_VERSION = "v1"
ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _peer_dict(comparison: PeerComparison | None) -> dict | None:
    if comparison is None:
        return None

    def row(r) -> dict:
        return {
            "company_name": r.company_name,
            "company_cik": r.company_cik,
            "company_ticker": r.company_ticker,
            "is_subject": r.is_subject,
            "period_end_date": _iso(r.period_end_date),
            "fiscal_year": r.fiscal_year,
            "fiscal_period": r.fiscal_period.value if r.fiscal_period else None,
            "end_offset_days": r.end_offset_days,
            "revenue": r.value(FinancialConcept.REVENUE),
            "net_income": r.value(FinancialConcept.NET_INCOME),
            "gross_profit": r.value(FinancialConcept.GROSS_PROFIT),
            "net_margin_pct": r.net_margin_pct,
            "gross_margin_pct": r.gross_margin_pct,
            "unavailable_reason": r.unavailable_reason,
        }

    return {
        "period_end_date": _iso(comparison.period_end_date),
        "fiscal_year": comparison.fiscal_year,
        "fiscal_period": comparison.fiscal_period.value,
        "max_offset_days": comparison.max_offset_days,
        "alignment_note": comparison.alignment_note(),
        "aligned_count": len(comparison.aligned_peers),
        "peer_count": len(comparison.peers),
        "widest_offset_days": comparison.widest_offset_days,
        "subject": row(comparison.subject),
        "peers": [row(r) for r in comparison.peers],
    }


@dataclass
class RunRecord:
    """One processed filing, flat enough to be a database row."""

    accession_number: str
    company_cik: str
    company_name: str
    company_ticker: str | None
    form: str
    filing_date: date
    report_date: date | None
    processed_at: str
    coverage_pct: float
    missing_concepts: list[str]
    elapsed_seconds: float
    memo_filename: str
    data_provenance_note: str
    rubric_version: str
    executive_summary: list[str] = field(default_factory=list)
    metric_rows: list[dict] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    peer_comparison: dict | None = None
    prior_narrative_accession: str | None = None
    # Delivery is best effort and recorded here rather than retried by
    # reprocessing: "pending" until attempted, then sent, skipped or failed.
    delivery_status: str = "pending"
    delivery_error: str | None = None
    delivered_at: str | None = None

    @property
    def notable_flag_count(self) -> int:
        return sum(1 for f in self.flags if f.get("severity") != "routine")

    @property
    def routine_flag_count(self) -> int:
        return sum(1 for f in self.flags if f.get("severity") == "routine")

    def summary(self) -> dict:
        """The listing view: enough to choose a filing, not the whole memo."""
        return {
            "accession_number": self.accession_number,
            "company_cik": self.company_cik,
            "company_name": self.company_name,
            "company_ticker": self.company_ticker,
            "form": self.form,
            "filing_date": self.filing_date.isoformat(),
            "report_date": _iso(self.report_date),
            "processed_at": self.processed_at,
            "coverage_pct": self.coverage_pct,
            "missing_concepts": list(self.missing_concepts),
            "notable_flags": self.notable_flag_count,
            "routine_flags": self.routine_flag_count,
            "delivery_status": self.delivery_status,
            "has_peer_comparison": self.peer_comparison is not None,
        }

    def to_dict(self) -> dict:
        payload = self.summary()
        payload.update({
            "version": RECORD_VERSION,
            "elapsed_seconds": self.elapsed_seconds,
            "memo_filename": self.memo_filename,
            "data_provenance_note": self.data_provenance_note,
            "rubric_version": self.rubric_version,
            "executive_summary": list(self.executive_summary),
            "metric_rows": list(self.metric_rows),
            "flags": list(self.flags),
            "peer_comparison": self.peer_comparison,
            "prior_narrative_accession": self.prior_narrative_accession,
            "delivery_status": self.delivery_status,
            "delivery_error": self.delivery_error,
            "delivered_at": self.delivered_at,
        })
        return payload

    @staticmethod
    def from_dict(value: dict[str, Any]) -> "RunRecord":
        version = value.get("version")
        if version != RECORD_VERSION:
            raise ValueError(f"Unsupported run record version {version!r}; expected {RECORD_VERSION!r}")
        report_date = value.get("report_date")
        return RunRecord(
            accession_number=str(value["accession_number"]),
            company_cik=str(value["company_cik"]),
            company_name=str(value["company_name"]),
            company_ticker=value.get("company_ticker"),
            form=str(value["form"]),
            filing_date=date.fromisoformat(value["filing_date"]),
            report_date=date.fromisoformat(report_date) if report_date else None,
            processed_at=str(value.get("processed_at", "")),
            coverage_pct=float(value.get("coverage_pct", 0.0)),
            missing_concepts=list(value.get("missing_concepts", [])),
            elapsed_seconds=float(value.get("elapsed_seconds", 0.0)),
            memo_filename=str(value.get("memo_filename", "")),
            data_provenance_note=str(value.get("data_provenance_note", "")),
            rubric_version=str(value.get("rubric_version", "")),
            executive_summary=list(value.get("executive_summary", [])),
            metric_rows=list(value.get("metric_rows", [])),
            flags=list(value.get("flags", [])),
            peer_comparison=value.get("peer_comparison"),
            prior_narrative_accession=value.get("prior_narrative_accession"),
            delivery_status=str(value.get("delivery_status", "pending")),
            delivery_error=value.get("delivery_error"),
            delivered_at=value.get("delivered_at"),
        )


def build_run_record(result, *, memo_filename: str, processed_at: str | None = None,
                     company_ticker: str | None = None) -> RunRecord:
    """
    Flatten a FilingResult into its stored form.

    The ticker comes from the watchlist rather than the filing: SEC filing
    events carry no ticker, and an alert headed "NVDA" is easier to scan in
    a channel than one headed "NVIDIA Corporation".
    """
    memo: Memo = result.memo
    event = result.event
    return RunRecord(
        accession_number=event.accession_number,
        company_cik=event.company_cik,
        company_name=memo.company_name or event.company_name,
        company_ticker=company_ticker or getattr(event, "company_ticker", None),
        form=event.form,
        filing_date=event.filing_date,
        report_date=event.report_date,
        processed_at=processed_at or datetime.now(timezone.utc).isoformat(),
        coverage_pct=result.coverage_pct,
        missing_concepts=[c.value for c in result.missing_concepts],
        elapsed_seconds=result.elapsed_seconds,
        memo_filename=memo_filename,
        data_provenance_note=memo.data_provenance_note,
        rubric_version=memo.rubric_version,
        executive_summary=list(memo.executive_summary),
        metric_rows=[{
            "concept": r.concept,
            "period_label": r.period_label,
            "comparison_label": r.comparison_label,
            "current_value": r.current_value,
            "percent_change": r.percent_change,
            "provenance": r.provenance,
            "comparison_value": r.comparison_value,
            "comparison_provenance": r.comparison_provenance,
            "citation": r.citation,
            "comparison_citation": r.comparison_citation,
        } for r in memo.metric_rows],
        flags=[{
            "rule_id": f.rule_id,
            "rule_description": f.rule_description,
            "severity": f.severity,
            "citation": f.citation,
            "detail": f.detail,
        } for f in memo.flags],
        peer_comparison=_peer_dict(memo.peer_comparison),
        prior_narrative_accession=result.prior_narrative_accession,
    )


def record_path(root: Path | str, accession_number: str) -> Path:
    if not ACCESSION_PATTERN.fullmatch(accession_number):
        raise ValueError(f"Invalid SEC accession number: {accession_number!r}")
    return Path(root) / f"{accession_number}.json"


def save_run_record(record: RunRecord, root: Path | str) -> Path:
    path = record_path(root, record.accession_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_run_record(root: Path | str, accession_number: str) -> RunRecord | None:
    path = record_path(root, accession_number)
    if not path.is_file():
        return None
    return RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_run_records(root: Path | str) -> list[RunRecord]:
    """
    Every readable record, newest filing first.

    An unreadable or future-version record is skipped, not raised on: one bad
    file must not make the whole listing unavailable. Ties on filing date
    break by accession so the order is stable across calls.
    """
    directory = Path(root)
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: (r.filing_date, r.accession_number), reverse=True)
    return records

"""
Persistence for screened filing narratives, so a later filing can be
compared against what the company said last time.

Same durability discipline as trigger/state_store.py: one JSON file per
filing, written through a temporary file and os.replace, so an interrupted
write cannot leave a half-written baseline that would silently corrupt the
next filing's comparison.

The `as of` rule matters as much here as it does in the XBRL path. A
comparison must only ever look at filings the company had ALREADY made when
the current one was filed. Reprocessing an old filing must not compare it
against a future one, or a rerun would produce different flags from the
original run.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from src.judgment.narrative import FilingNarrative, form_class

ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")


def _company_dir(root: Path | str, company_cik: str) -> Path:
    if not company_cik.isdigit():
        raise ValueError(f"Company CIK must be numeric: {company_cik!r}")
    return Path(root) / company_cik


def narrative_path(root: Path | str, company_cik: str, accession_number: str) -> Path:
    if not ACCESSION_PATTERN.fullmatch(accession_number):
        raise ValueError(f"Invalid SEC accession number: {accession_number!r}")
    return _company_dir(root, company_cik) / f"{accession_number}.json"


def save_narrative(narrative: FilingNarrative, root: Path | str) -> Path:
    path = narrative_path(root, narrative.company_cik, narrative.accession_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(narrative.to_dict(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_narrative(root: Path | str, company_cik: str, accession_number: str) -> FilingNarrative | None:
    path = narrative_path(root, company_cik, accession_number)
    if not path.is_file():
        return None
    return FilingNarrative.from_dict(json.loads(path.read_text(encoding="utf-8")))


def stored_accessions(root: Path | str, company_cik: str) -> set[str]:
    """
    Which of this company's filings already have a baseline. Used to decide
    whether seeding is needed at all, so a re-run does not refetch filings it
    has already screened.
    """
    directory = _company_dir(root, company_cik)
    if not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.json")}


def has_narrative(root: Path | str, company_cik: str) -> bool:
    """True if any baseline exists for this company."""
    return bool(stored_accessions(root, company_cik))


def load_prior_narrative(
    root: Path | str,
    company_cik: str,
    *,
    before: date,
    exclude_accession: str | None = None,
    comparable_to: str | None = None,
) -> FilingNarrative | None:
    """
    The company's most recent stored filing from strictly before `before`.

    `comparable_to` is the form of the filing being processed; only baselines
    of the same class are considered, so a 10-Q is never compared against an
    8-K filed between it and the previous 10-Q.

    Ties on filing date are broken by accession number so the choice of
    baseline is deterministic when a company files twice in one day. A
    record that cannot be read is skipped rather than raising: a corrupt or
    future-version baseline should degrade the comparison to
    "no prior filing on record", not fail the filing being processed.
    """
    directory = _company_dir(root, company_cik)
    if not directory.is_dir():
        return None
    candidates = []
    for path in sorted(directory.glob("*.json")):
        try:
            narrative = FilingNarrative.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if narrative.accession_number == exclude_accession:
            continue
        if narrative.filing_date >= before:
            continue
        if comparable_to is not None and form_class(narrative.form) != form_class(comparable_to):
            continue
        if not narrative.passages:
            # A baseline with nothing screened cannot establish that anything
            # changed, and picking it would hide a usable older one.
            continue
        candidates.append(narrative)
    if not candidates:
        return None
    return max(candidates, key=lambda n: (n.filing_date, n.accession_number))

import json
from datetime import date

import pytest

from src.judgment.narrative import FilingNarrative, NarrativePassage
from src.judgment.narrative_store import (
    load_narrative,
    load_prior_narrative,
    narrative_path,
    save_narrative,
)

CIK = "0000320193"


def _narrative(accession: str, filing_date: date, text: str = "A lawsuit was filed.") -> FilingNarrative:
    return FilingNarrative(
        company_cik=CIK, accession_number=accession, form="10-Q", filing_date=filing_date,
        report_date=None,
        passages=[NarrativePassage("p1", "Item 1A. Risk Factors", text, ("LITIGATION",))],
    )


def test_save_and_load_round_trip(tmp_path):
    original = _narrative("0000320193-24-000020", date(2024, 7, 20))
    save_narrative(original, tmp_path)
    assert load_narrative(tmp_path, CIK, "0000320193-24-000020") == original


def test_loading_an_absent_record_returns_none(tmp_path):
    assert load_narrative(tmp_path, CIK, "0000320193-24-000020") is None


def test_save_replaces_an_earlier_record_in_place(tmp_path):
    save_narrative(_narrative("0000320193-24-000020", date(2024, 7, 20), "first"), tmp_path)
    save_narrative(_narrative("0000320193-24-000020", date(2024, 7, 20), "second"), tmp_path)
    loaded = load_narrative(tmp_path, CIK, "0000320193-24-000020")
    assert loaded.passages[0].text == "second"
    # No temporary files survive the atomic write.
    assert [p.name for p in (tmp_path / CIK).iterdir()] == ["0000320193-24-000020.json"]


def test_records_are_separated_by_company(tmp_path):
    save_narrative(_narrative("0000320193-24-000020", date(2024, 7, 20)), tmp_path)
    other = FilingNarrative(
        company_cik="0000789019", accession_number="0000789019-24-000005", form="10-Q",
        filing_date=date(2024, 7, 20),
        passages=[NarrativePassage("p1", "Item 1A. Risk Factors", "A lawsuit was filed.",
                                   ("LITIGATION",))])
    save_narrative(other, tmp_path)
    assert load_prior_narrative(tmp_path, "0000789019", before=date(2024, 12, 31)).accession_number == \
        "0000789019-24-000005"


def test_malformed_accession_and_cik_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid SEC accession number"):
        narrative_path(tmp_path, CIK, "not-an-accession")
    with pytest.raises(ValueError, match="Company CIK must be numeric"):
        narrative_path(tmp_path, "../escape", "0000320193-24-000020")


# --- prior lookup ------------------------------------------------------------

def test_prior_lookup_returns_the_most_recent_earlier_filing(tmp_path):
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    save_narrative(_narrative("0000320193-24-000020", date(2024, 4, 20)), tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20))
    assert prior.accession_number == "0000320193-24-000020"


def test_prior_lookup_never_sees_a_future_filing(tmp_path):
    # Reprocessing an old filing must not compare it against one filed later,
    # or a rerun would not reproduce the original run's flags.
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    save_narrative(_narrative("0000320193-24-000030", date(2024, 10, 20)), tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 4, 20))
    assert prior.accession_number == "0000320193-24-000010"


def test_a_filing_on_the_same_day_is_not_its_own_baseline(tmp_path):
    save_narrative(_narrative("0000320193-24-000020", date(2024, 4, 20)), tmp_path)
    assert load_prior_narrative(tmp_path, CIK, before=date(2024, 4, 20)) is None


def test_the_filing_being_processed_is_excluded(tmp_path):
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    save_narrative(_narrative("0000320193-24-000020", date(2024, 4, 20)), tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20),
                                 exclude_accession="0000320193-24-000020")
    assert prior.accession_number == "0000320193-24-000010"


def test_same_day_filings_break_ties_deterministically(tmp_path):
    save_narrative(_narrative("0000320193-24-000011", date(2024, 4, 20)), tmp_path)
    save_narrative(_narrative("0000320193-24-000022", date(2024, 4, 20)), tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20))
    assert prior.accession_number == "0000320193-24-000022"


def test_no_records_for_a_company_returns_none(tmp_path):
    assert load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20)) is None


def test_a_corrupt_baseline_degrades_to_no_prior_rather_than_failing(tmp_path):
    # A damaged baseline must not take down the filing being processed.
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    (tmp_path / CIK / "0000320193-24-000015.json").write_text("{ not json", encoding="utf-8")
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20))
    assert prior.accession_number == "0000320193-24-000010"


def test_a_future_version_baseline_is_skipped(tmp_path):
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    payload = _narrative("0000320193-24-000015", date(2024, 2, 20)).to_dict()
    payload["version"] = "v99"
    (tmp_path / CIK / "0000320193-24-000015.json").write_text(json.dumps(payload), encoding="utf-8")
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20))
    assert prior.accession_number == "0000320193-24-000010"


def test_every_baseline_being_unreadable_returns_none(tmp_path):
    (tmp_path / CIK).mkdir(parents=True)
    (tmp_path / CIK / "0000320193-24-000015.json").write_text("{ not json", encoding="utf-8")
    assert load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20)) is None


def test_an_empty_baseline_is_skipped_for_a_usable_older_one(tmp_path):
    # A filing with nothing screened cannot establish that anything changed,
    # and picking it would hide a baseline that can.
    save_narrative(_narrative("0000320193-24-000010", date(2024, 1, 20)), tmp_path)
    empty = FilingNarrative(company_cik=CIK, accession_number="0000320193-24-000015",
                            form="10-Q", filing_date=date(2024, 2, 20), passages=[])
    save_narrative(empty, tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20))
    assert prior.accession_number == "0000320193-24-000010"


def test_a_periodic_filing_is_not_compared_against_an_event_filing(tmp_path):
    # An 8-K is a short event filing; comparing a 10-Q against one finds almost
    # nothing in common and reports the whole filing as new.
    save_narrative(_narrative("0000320193-24-000010", date(2024, 4, 20)), tmp_path)
    event_filing = FilingNarrative(
        company_cik=CIK, accession_number="0000320193-24-000018", form="8-K",
        filing_date=date(2024, 7, 1),
        passages=[NarrativePassage("p1", "Item 8.01", "A lawsuit update.", ("LITIGATION",))])
    save_narrative(event_filing, tmp_path)

    periodic = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20), comparable_to="10-Q")
    assert periodic.accession_number == "0000320193-24-000010"

    # And an 8-K compares against the 8-K, not the 10-Q.
    event = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20), comparable_to="8-K")
    assert event.accession_number == "0000320193-24-000018"


def test_amendments_share_their_base_form_class(tmp_path):
    save_narrative(_narrative("0000320193-24-000010", date(2024, 4, 20)), tmp_path)
    prior = load_prior_narrative(tmp_path, CIK, before=date(2024, 7, 20), comparable_to="10-Q/A")
    assert prior.accession_number == "0000320193-24-000010"

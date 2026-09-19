from datetime import date

import pytest

from src.judgment.narrative import (
    MAX_FLAGS_PER_TOPIC,
    FilingNarrative,
    NarrativePassage,
    compare_narrative,
    flag_narrative_changes,
    normalize_for_comparison,
    screen_filing_narrative,
)
from src.retrieval.chunking import Chunk

BOILERPLATE = (
    "A former supplier filed a lawsuit against the Company on May 14, 2024, alleging breach of "
    "contract. The Company believes the claims are without merit and intends to defend itself "
    "vigorously. The outcome of litigation is inherently uncertain."
)


def _chunk(chunk_id: str, text: str, kind: str = "prose", section: str = "Item 1A. Risk Factors") -> Chunk:
    return Chunk(chunk_id=chunk_id, section=section, text=text, kind=kind, source_orders=[0])


def _narrative(*passages: NarrativePassage, accession="0000000000-24-000001", filing_date=date(2024, 5, 1)):
    return FilingNarrative(company_cik="0000320193", accession_number=accession, form="10-Q",
                           filing_date=filing_date, report_date=None, passages=list(passages))


def _passage(chunk_id: str, text: str, topics=("LITIGATION",)) -> NarrativePassage:
    return NarrativePassage(chunk_id=chunk_id, section="Item 1A. Risk Factors", text=text, topics=tuple(topics))


# --- normalization -----------------------------------------------------------

def test_dates_are_folded_so_rolled_forward_boilerplate_matches():
    q1 = "As of March 31, 2024 there have been no material changes."
    q2 = "As of June 30, 2024 there have been no material changes."
    assert normalize_for_comparison(q1) == normalize_for_comparison(q2)


def test_iso_and_fiscal_year_references_are_folded_too():
    assert normalize_for_comparison("ended 2024-06-30") == normalize_for_comparison("ended 2025-09-28")
    assert normalize_for_comparison("in fiscal 2024") == normalize_for_comparison("in fiscal 2025")


def test_monetary_amounts_are_not_folded():
    # A covenant ratio or an accrual moving is a real change, not a rolled date.
    a = normalize_for_comparison("maintain a leverage ratio below 3.5x")
    b = normalize_for_comparison("maintain a leverage ratio below 4.0x")
    assert a != b


def test_case_and_whitespace_collapse():
    assert normalize_for_comparison("  The   COMPANY \n was  sued ") == "the company was sued"


# --- screening ---------------------------------------------------------------

def test_screens_prose_by_topic_keyword():
    narrative = screen_filing_narrative(
        [_chunk("p1", BOILERPLATE),
         _chunk("p2", "Revenue increased due to automotive demand."),
         _chunk("p3", "We must maintain a minimum liquidity covenant under the credit agreement.")],
        company_cik="0000320193", accession_number="0000320193-24-000020", form="10-Q",
        filing_date=date(2024, 7, 20),
    )
    found = {p.chunk_id: p.topics for p in narrative.passages}
    assert found == {"p1": ("LITIGATION",), "p3": ("LIQUIDITY_REGULATORY",)}


def test_a_passage_can_carry_both_topics():
    narrative = screen_filing_narrative(
        [_chunk("p1", "The lawsuit could trigger a default under our debt covenant.")],
        company_cik="0000320193", accession_number="0000320193-24-000020", form="10-Q",
        filing_date=date(2024, 7, 20),
    )
    assert narrative.passages[0].topics == ("LIQUIDITY_REGULATORY", "LITIGATION")


def test_tables_are_never_screened():
    # A keyword in a flattened table rendering is a column label, not a disclosure.
    narrative = screen_filing_narrative(
        [_chunk("t1", "Litigation reserve | 1,200 | 900", kind="table")],
        company_cik="0000320193", accession_number="0000320193-24-000020", form="10-Q",
        filing_date=date(2024, 7, 20),
    )
    assert narrative.passages == []


# --- comparison --------------------------------------------------------------

def test_rolled_forward_boilerplate_is_unchanged():
    prior = _narrative(_passage("p1", BOILERPLATE))
    current = _narrative(_passage("p9", BOILERPLATE.replace("May 14, 2024", "August 2, 2024")))
    change = compare_narrative(current, prior)[0]
    assert change.status == "unchanged"
    assert change.prior_chunk_id == "p1"


def test_edited_passage_is_revised_and_cites_the_edit():
    prior = _narrative(_passage("p1", BOILERPLATE))
    current = _narrative(_passage("p1", BOILERPLATE + " The court denied the motion to dismiss."))
    change = compare_narrative(current, prior)[0]
    assert change.status == "revised"
    assert "denied the motion to dismiss" in change.diff_summary
    assert change.prior_chunk_id == "p1"


def test_unrelated_passage_is_new():
    prior = _narrative(_passage("p1", BOILERPLATE))
    current = _narrative(_passage("p2", "The SEC has opened a regulatory investigation into our "
                                        "revenue recognition practices for the data center segment."))
    change = compare_narrative(current, prior)[0]
    assert change.status == "new"
    # "new" means no counterpart, so there is no prior passage to point at.
    assert change.prior_chunk_id is None


def test_no_prior_filing_yields_unestablished_not_new():
    current = _narrative(_passage("p1", BOILERPLATE))
    for prior in (None, _narrative()):
        change = compare_narrative(current, prior)[0]
        assert change.status == "unestablished"
        assert change.similarity is None


def test_best_match_wins_across_several_prior_passages():
    prior = _narrative(
        _passage("far", "We lease office space in several countries."),
        _passage("near", BOILERPLATE),
    )
    current = _narrative(_passage("c1", BOILERPLATE))
    assert compare_narrative(current, prior)[0].prior_chunk_id == "near"


# --- flags -------------------------------------------------------------------

def test_unchanged_language_is_routine_and_new_is_notable():
    prior = _narrative(_passage("p1", BOILERPLATE))
    current = _narrative(
        _passage("p1", BOILERPLATE),
        _passage("p2", "A class action was filed concerning our disclosure of supply constraints."),
    )
    flags = {f.rule_id: f for f in flag_narrative_changes(compare_narrative(current, prior), prior)}
    assert flags["UNCHANGED_LITIGATION_LANGUAGE"].severity == "routine"
    assert flags["NEW_LITIGATION_LANGUAGE"].severity == "notable"


def test_flags_name_the_baseline_filing():
    prior = _narrative(_passage("p1", BOILERPLATE), accession="0000320193-24-000010",
                       filing_date=date(2024, 4, 1))
    current = _narrative(_passage("p1", BOILERPLATE))
    flag = flag_narrative_changes(compare_narrative(current, prior), prior)[0]
    assert "0000320193-24-000010" in flag.rule_description
    assert "2024-04-01" in flag.rule_description


def test_flags_say_so_when_there_is_no_baseline():
    current = _narrative(_passage("p1", BOILERPLATE))
    flag = flag_narrative_changes(compare_narrative(current, None), None)[0]
    assert flag.rule_id == "UNESTABLISHED_LITIGATION_LANGUAGE"
    assert "no prior filing on record" in flag.rule_description


def test_every_flag_carries_a_citation_to_its_passage():
    prior = _narrative(_passage("p1", BOILERPLATE))
    current = _narrative(_passage("p7", BOILERPLATE + " New sentence entirely about something else."))
    for flag in flag_narrative_changes(compare_narrative(current, prior), prior):
        assert "p7" in flag.citation
        assert flag.rule_description and flag.detail


def test_per_topic_cap_drops_routine_repeats_before_notable_ones():
    prior = _narrative(*[_passage(f"p{i}", f"{BOILERPLATE} Variation {i}.") for i in range(MAX_FLAGS_PER_TOPIC + 5)])
    current = _narrative(
        *[_passage(f"p{i}", f"{BOILERPLATE} Variation {i}.") for i in range(MAX_FLAGS_PER_TOPIC + 5)],
        _passage("brand-new", "An entirely separate class action complaint was served on the Company "
                              "by a group of former distributors seeking unspecified damages."),
    )
    flags = flag_narrative_changes(compare_narrative(current, prior), prior)
    assert len(flags) == MAX_FLAGS_PER_TOPIC
    assert any(f.rule_id == "NEW_LITIGATION_LANGUAGE" for f in flags)


def test_relevance_order_breaks_ties_within_a_status():
    prior = _narrative()
    current = _narrative(
        _passage("a", "A lawsuit was filed by party one."),
        _passage("b", "A lawsuit was filed by party two."),
    )
    changes = compare_narrative(current, prior)
    ranked = flag_narrative_changes(changes, prior, relevance_order=["b", "a"], max_per_topic=1)
    assert ranked[0].citation.startswith("Item 1A. Risk Factors (b)")
    # Without a ranking the order is deterministic by chunk id, not arbitrary.
    unranked = flag_narrative_changes(changes, prior, max_per_topic=1)
    assert unranked[0].citation.startswith("Item 1A. Risk Factors (a)")


def test_relevance_order_cannot_suppress_a_more_significant_status():
    # Eligibility is the rules' call; retrieval only orders within a status.
    prior = _narrative(_passage("boiler", BOILERPLATE))
    current = _narrative(
        _passage("boiler", BOILERPLATE),
        _passage("fresh", "A newly served subpoena from state regulators concerns our export controls."),
    )
    flags = flag_narrative_changes(compare_narrative(current, prior), prior,
                                   relevance_order=["boiler"], max_per_topic=1)
    assert flags[0].rule_id == "NEW_LITIGATION_LANGUAGE"


# --- serialization -----------------------------------------------------------

def test_narrative_round_trips_through_its_dict_form():
    original = _narrative(_passage("p1", BOILERPLATE, topics=("LITIGATION", "LIQUIDITY_REGULATORY")))
    restored = FilingNarrative.from_dict(original.to_dict())
    assert restored == original


def test_unknown_record_version_is_rejected():
    payload = _narrative(_passage("p1", BOILERPLATE)).to_dict()
    payload["version"] = "v99"
    with pytest.raises(ValueError, match="Unsupported narrative record version"):
        FilingNarrative.from_dict(payload)


# --- robustness to re-chunking ----------------------------------------------

SENT_A = "The Company maintains a revolving credit facility with a financial covenant requiring a leverage ratio below 3.5 times."
SENT_B = "A former distributor filed a complaint alleging breach of contract in the Northern District of California."
SENT_C = "Management believes the claims are without merit and intends to defend the matter vigorously."
SENT_NEW = "In August 2026 the Company guaranteed up to 105 billion dollars of a partner data center buildout."


def test_reshuffled_chunk_boundaries_still_read_as_unchanged():
    # Chunk boundaries follow a size budget, so one edit upstream moves every
    # boundary after it. The same sentences split differently must not read as
    # new language - this is the failure the sentence-level comparison exists
    # to prevent.
    prior = _narrative(
        _passage("p1", f"{SENT_A} {SENT_B}"),
        _passage("p2", SENT_C),
    )
    current = _narrative(
        _passage("q1", SENT_A),
        _passage("q2", f"{SENT_B} {SENT_C}"),
    )
    statuses = {c.passage.chunk_id: c.status for c in compare_narrative(current, prior)}
    assert statuses == {"q1": "unchanged", "q2": "unchanged"}


def test_added_sentence_makes_a_repacked_passage_revised_and_is_quoted():
    prior = _narrative(_passage("p1", f"{SENT_A} {SENT_B}"), _passage("p2", SENT_C))
    current = _narrative(_passage("q1", f"{SENT_B} {SENT_C} {SENT_NEW}"))
    change = compare_narrative(current, prior)[0]
    assert change.status == "revised"
    assert "105 billion" in change.diff_summary
    # The sentences that were already there are not reported as the change.
    assert SENT_B.lower()[:40] not in change.diff_summary


def test_a_wholly_new_passage_is_still_new_under_coverage_scoring():
    prior = _narrative(_passage("p1", f"{SENT_A} {SENT_B}"))
    current = _narrative(_passage("q1", SENT_NEW + " The guarantee is capped and disclosed in Note 11 of this report."))
    assert compare_narrative(current, prior)[0].status == "new"


def test_coverage_is_reported_as_the_similarity_score():
    prior = _narrative(_passage("p1", f"{SENT_A} {SENT_B}"))
    current = _narrative(_passage("q1", f"{SENT_A} {SENT_B} {SENT_NEW} {SENT_C}"))
    change = compare_narrative(current, prior)[0]
    assert change.similarity == 0.5  # two of four sentences already present
    assert change.status == "revised"


def test_short_sentences_do_not_carry_the_verdict():
    # "Refer to Note 11." matches almost anything; it must not make a passage
    # look unchanged on its own.
    prior = _narrative(_passage("p1", "Refer to Note 11."))
    current = _narrative(_passage("q1", f"Refer to Note 11. {SENT_NEW}"))
    assert compare_narrative(current, prior)[0].status == "new"

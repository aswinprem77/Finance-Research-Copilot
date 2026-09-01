import json
import tempfile
from pathlib import Path

from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.trigger.trigger_agent import poll_watchlist_for_new_filings

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_submissions.json"


def _one_company_watchlist() -> Watchlist:
    return Watchlist(
        sector="Test",
        companies=[WatchlistCompany(name="Synthetic Test Co", ticker="SYNT", cik="0000320193")],
    )


def _stand_in_fetch(cik: str) -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_first_poll_returns_all_relevant_filings():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        events = poll_watchlist_for_new_filings(_one_company_watchlist(), _stand_in_fetch, state_path)
        assert len(events) == 3  # 10-Q, 8-K, 10-K -- the Form 4 is filtered upstream


def test_second_poll_with_same_data_returns_nothing_new():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        first = poll_watchlist_for_new_filings(_one_company_watchlist(), _stand_in_fetch, state_path)
        second = poll_watchlist_for_new_filings(_one_company_watchlist(), _stand_in_fetch, state_path)
        assert len(first) == 3
        assert second == []


def test_state_persists_across_separate_calls_to_the_same_path():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        poll_watchlist_for_new_filings(_one_company_watchlist(), _stand_in_fetch, state_path)
        assert state_path.exists()
        # A fresh poll call (simulating a new scheduled run) against the same state file
        # still correctly recognizes everything as already-seen.
        third = poll_watchlist_for_new_filings(_one_company_watchlist(), _stand_in_fetch, state_path)
        assert third == []


def _stand_in_fetch_distinct_per_company(cik: str) -> dict:
    """
    Real SEC accession numbers are prefixed by the filing company's own CIK, so two
    different companies never share a literal accession number. Reusing the shared
    fixture's numbers verbatim for two different CIKs would create a collision that
    could never happen with real data -- this synthesizes genuinely distinct numbers
    per company instead, by swapping in the actual CIK being fetched.
    """
    raw = json.loads(FIXTURE_PATH.read_text())
    recent = raw["filings"]["recent"]
    recent["accessionNumber"] = [f"{cik}-{acc.split('-', 1)[1]}" for acc in recent["accessionNumber"]]
    return raw


def test_multi_company_watchlist_polls_each_company():
    watchlist = Watchlist(
        sector="Test",
        companies=[
            WatchlistCompany(name="Company A", ticker="AAA", cik="0000320193"),
            WatchlistCompany(name="Company B", ticker="BBB", cik="0001111111"),
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        events = poll_watchlist_for_new_filings(watchlist, _stand_in_fetch_distinct_per_company, state_path)
        # Each company has its own (synthesized-distinct) accession numbers -- 3 relevant
        # filings x 2 companies = 6, none lost to cross-company collision.
        assert len(events) == 6
        assert {e.company_cik for e in events} == {"0000320193", "0001111111"}


def test_identical_accession_numbers_across_companies_would_collide():
    # Documents WHY the fetch stand-in above matters: if two companies' fetch results
    # really did share literal accession numbers (which can't happen with real SEC
    # data, but confirms the seen-set's actual behavior either way), the second
    # company's overlapping filings are correctly treated as already-seen, since
    # accession number is the only identity key this module tracks by design.
    watchlist = Watchlist(
        sector="Test",
        companies=[
            WatchlistCompany(name="Company A", ticker="AAA", cik="0000320193"),
            WatchlistCompany(name="Company B", ticker="BBB", cik="0001111111"),
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        events = poll_watchlist_for_new_filings(watchlist, _stand_in_fetch, state_path)
        assert len(events) == 3  # not 6 -- confirms seen-state keys strictly on accession number

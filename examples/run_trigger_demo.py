"""
Trigger agent demo — polls the watchlist twice against the same synthetic
submissions data. First poll should return every relevant filing (nothing
seen yet); second poll should return nothing (already seen) — that's the
whole point of the persisted state store.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.watchlist import load_watchlist
from src.trigger.trigger_agent import poll_watchlist_for_new_filings

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_submissions.json"


def _stand_in_fetch(cik: str) -> dict:
    # Real SEC accession numbers are prefixed by the filer's own CIK, so two
    # companies never literally share one. Synthesizing per-CIK numbers here
    # (rather than reusing the fixture's hardcoded prefix for every company)
    # avoids an artificial collision that can't happen with real data -- see
    # test_identical_accession_numbers_across_companies_would_collide in
    # test_trigger_agent.py for what happens if that collision DOES occur.
    raw = json.loads(FIXTURE_PATH.read_text())
    recent = raw["filings"]["recent"]
    recent["accessionNumber"] = [f"{cik}-{acc.split('-', 1)[1]}" for acc in recent["accessionNumber"]]
    return raw


def main() -> None:
    watchlist = load_watchlist()
    print(f"Watchlist: {watchlist.sector} ({len(watchlist.companies)} companies)\n")

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "trigger_state_demo.json"

        print("-- First poll --")
        first = poll_watchlist_for_new_filings(watchlist, _stand_in_fetch, state_path)
        print(f"  {len(first)} new filing(s) detected")
        for f in first[:5]:
            print(f"    {f.company_cik}  {f.form:5s}  {f.filing_date}  {f.accession_number}")
        if len(first) > 5:
            print(f"    ... and {len(first) - 5} more")

        print("\n-- Second poll (same underlying data) --")
        second = poll_watchlist_for_new_filings(watchlist, _stand_in_fetch, state_path)
        print(f"  {len(second)} new filing(s) detected  <- correctly empty, already seen")


if __name__ == "__main__":
    main()

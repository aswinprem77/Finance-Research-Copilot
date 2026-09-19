"""Run with python -m src.pipeline --demo, or --once for a live poll."""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from src.ingestion.xbrl_client import SecXbrlClient
from src.pipeline.runner import run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany, load_watchlist

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description="Poll SEC filings and save cited Markdown memos")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Offline synthetic end-to-end run")
    mode.add_argument("--once", action="store_true", help="One live poll (default)")
    mode.add_argument("--interval", type=float, help="Repeat live polls every N seconds (minimum 60)")
    parser.add_argument("--since", type=date.fromisoformat, default=date.today() - timedelta(days=7))
    parser.add_argument("--watchlist", type=Path, default=ROOT / "config/watchlist.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--state-path", type=Path)
    args = parser.parse_args()
    if args.interval is not None and args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.demo:
        fixtures = ROOT / "tests/fixtures"
        raw = json.loads((fixtures / "sample_companyfacts.json").read_text(encoding="utf-8"))
        html = (fixtures / "sample_filing_excerpt.html").read_text(encoding="utf-8")
        watchlist = Watchlist("Synthetic", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")])
        submissions = {"name": "SYNTHETIC TEST CO", "filings": {"recent": {
            "accessionNumber": ["0000320193-24-000020"], "form": ["10-Q"],
            "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"],
        }}}
        fetch_submissions = lambda cik: submissions
        fetch_facts = lambda cik: raw
        fetch_html = lambda event: html
        note = "SYNTHETIC TEST FIXTURE; not a real company or filing."
        since = None
        directory = ROOT / "data/demo"
    else:
        load_dotenv(ROOT / ".env")
        try:
            client = SecXbrlClient(os.getenv("SEC_USER_AGENT", ""))
        except ValueError as exc:
            parser.error(str(exc))
        watchlist = load_watchlist(args.watchlist)
        fetch_submissions = client.get_submissions
        fetch_facts = lambda cik: client.get_company_facts(cik, use_cache=False)
        fetch_html = client.get_filing_html
        note = "SEC companyfacts and primary filing HTML, retrieved for this run."
        since = args.since
        directory = ROOT / "data/live"
    try:
        while True:
            result = run_poll_cycle(watchlist, fetch_submissions, fetch_facts, fetch_html,
                state_path=args.state_path or directory / "completed.json",
                output_dir=args.output_dir or directory / "memos", data_provenance_note=note, since=since)
            print(json.dumps({"completed": [str(p) for p in result.completed], "errors": result.errors}, indent=2))
            if args.interval is None:
                return 1 if result.errors else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

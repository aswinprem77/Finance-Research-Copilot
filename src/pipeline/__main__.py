"""Run with python -m src.pipeline --demo, or --once for a live poll."""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from src.ingestion.xbrl_client import SecXbrlClient
from src.pipeline.backfill import (
    DEFAULT_DEPTH,
    backfill_narrative_baselines,
    companies_without_baselines,
)
from src.pipeline.runner import run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany, load_watchlist
from src.retrieval.providers import PROFILES, build_retrieval_stack
from src.service.notify import NullNotifier, build_notifier_from_env, deliver_pending

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description="Poll SEC filings and save cited Markdown memos")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Offline synthetic end-to-end run")
    mode.add_argument("--once", action="store_true", help="One live poll (default)")
    mode.add_argument("--interval", type=float, help="Repeat live polls every N seconds (minimum 60)")
    mode.add_argument("--notify-pending", action="store_true",
                      help="Retry delivery for stored records that were never sent. Reads records "
                           "only: no SEC calls and no reprocessing.")
    mode.add_argument("--backfill", action="store_true",
                      help="Seed narrative baselines from each company's most recent past filing, so "
                           "the next real filing is compared instead of reported as unestablished. "
                           "Writes no memos and does not mark anything completed.")
    parser.add_argument("--since", type=date.fromisoformat, default=date.today() - timedelta(days=7))
    parser.add_argument("--watchlist", type=Path, default=ROOT / "config/watchlist.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--records-dir", type=Path,
                        help="Where structured run records are written. These are what the API serves.")
    parser.add_argument("--narrative-dir", type=Path,
                        help="Where prior-filing narrative baselines are kept. Language flags can "
                             "only establish change against filings stored here.")
    parser.add_argument("--backfill-depth", type=int, default=DEFAULT_DEPTH,
                        help="Filings to seed per company. Two covers both the next filing to "
                             "arrive and the most recent one already published; more is for "
                             "reprocessing history.")
    parser.add_argument("--no-notify", action="store_true",
                        help="Process filings without sending alerts.")
    parser.add_argument("--force", action="store_true",
                        help="With --backfill, re-seed companies that already have baselines.")
    parser.add_argument("--retrieval-profile", choices=PROFILES,
                        help="Path B stack. Default: semantic, or RETRIEVAL_PROFILE if set. "
                             "The offline demo defaults to lexical so it needs no model download.")
    args = parser.parse_args()
    if args.interval is not None and args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.backfill_depth < 1:
        parser.error("--backfill-depth must be at least 1")

    if args.notify_pending:
        load_dotenv(ROOT / ".env")
        records_dir = args.records_dir or ROOT / "data/live/records"
        notifier = build_notifier_from_env()
        if isinstance(notifier, NullNotifier):
            parser.error("No notifier configured. Set SLACK_WEBHOOK_URL, or SMTP_HOST with "
                         "NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO, in .env.")
        outcome = deliver_pending(records_dir, notifier)
        print(json.dumps({"records_dir": str(records_dir), "notifier": notifier.name,
                          **outcome}, indent=2))
        return 1 if outcome["failed"] else 0

    if args.backfill:
        load_dotenv(ROOT / ".env")
        try:
            client = SecXbrlClient(os.getenv("SEC_USER_AGENT", ""))
        except ValueError as exc:
            parser.error(str(exc))
        narrative_dir = args.narrative_dir or ROOT / "data/live/narrative"
        result = backfill_narrative_baselines(
            load_watchlist(args.watchlist), client.get_submissions, client.get_filing_html,
            narrative_dir=narrative_dir, depth=args.backfill_depth, force=args.force)
        print(json.dumps({"narrative_dir": str(narrative_dir), **result.to_dict()}, indent=2))
        return 1 if result.errors else 0

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
        profile = args.retrieval_profile or os.getenv("RETRIEVAL_PROFILE") or "lexical"
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
        profile = args.retrieval_profile
    try:
        retrieval = build_retrieval_stack(profile)
    except ValueError as exc:
        parser.error(str(exc))

    notifier = NullNotifier() if args.no_notify else build_notifier_from_env()
    if isinstance(notifier, NullNotifier):
        print("Alerts: disabled. Memos are written but nothing is sent.")
    else:
        print(f"Alerts: {notifier.name}, for filings with at least one notable finding.")

    narrative_dir = args.narrative_dir or directory / "narrative"
    # Say this up front rather than letting the user find every language flag
    # reported as "unestablished" in the memos and wonder why.
    missing = companies_without_baselines(watchlist, narrative_dir)
    if missing:
        print(f"Note: no narrative baseline for {', '.join(missing)}. Their language flags cannot "
              f"establish change until a second filing is processed. Run `--backfill` to seed them.")
    try:
        while True:
            result = run_poll_cycle(watchlist, fetch_submissions, fetch_facts, fetch_html,
                state_path=args.state_path or directory / "completed.json",
                output_dir=args.output_dir or directory / "memos", data_provenance_note=note, since=since,
                retrieval=retrieval, narrative_dir=narrative_dir,
                records_dir=args.records_dir or directory / "records", notifier=notifier)
            print(json.dumps({"completed": [str(p) for p in result.completed], "errors": result.errors,
                              "delivery_failures": result.delivery_failures}, indent=2))
            if args.interval is None:
                return 1 if result.errors else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

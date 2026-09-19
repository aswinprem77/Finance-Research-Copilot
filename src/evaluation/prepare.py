"""Prepare real SEC filings for manual benchmark labeling.

This module proposes cases; it never promotes system output to ground truth.
Reviewers must fill the null verification/relevance fields using the linked filing.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from src.ingestion.xbrl_client import SecXbrlClient
from src.pipeline.runner import process_filing, write_memo
from src.pipeline.watchlist import Watchlist, load_watchlist
from src.retrieval.chunking import chunk_blocks
from src.retrieval.html_ingest import parse_filing_html
from src.retrieval.hybrid_index import HybridIndex
from src.retrieval.providers import PROFILES, RetrievalStack, build_retrieval_stack
from src.trigger.edgar_client import FilingEvent, parse_recent_filings

ROOT = Path(__file__).resolve().parents[2]
REVIEW_QUERIES = [
    ("risk_changes", "material changes risk factors new risks"),
    ("litigation", "lawsuit litigation complaint regulatory investigation"),
    ("liquidity", "liquidity cash flow debt covenant default going concern"),
    ("revenue_drivers", "revenue change drivers demand volume pricing customers"),
    ("margin_drivers", "gross margin operating margin change drivers costs"),
]


def _source_url(event: FilingEvent) -> str:
    accession = event.accession_number
    return (f"https://www.sec.gov/Archives/edgar/data/{int(event.company_cik)}/"
            f"{accession.replace('-', '')}/{accession}-index.html")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


def _numeric_review(rows) -> list[dict]:
    reviews = []
    seen = set()
    for row in rows:
        key = (row.concept, row.period_label, row.current_value, row.provenance, row.citation)
        if key in seen:
            continue
        seen.add(key)
        reviews.append({
            "concept": row.concept,
            "period": row.period_label,
            "system_value": row.current_value,
            "source": row.provenance,
            "citation": row.citation,
            "verified_value": None,
            "verified": None,
            "reviewer_notes": "",
        })
    return reviews


def _dedupe_existing_numeric(items: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for item in items:
        key = (item["concept"], item["period"], item["system_value"], item["source"], item["citation"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_review_files(queue: dict, output: Path | str) -> dict:
    """Normalize a queue and export spreadsheets suitable for manual labeling."""
    output = Path(output)
    numeric_rows = []
    retrieval_rows = []
    summary_rows = []
    for filing in queue["filings"]:
        filing["numeric_review"] = _dedupe_existing_numeric(filing["numeric_review"])
        company = filing["company"]
        metadata = filing["filing"]
        common = {
            "ticker": company["ticker"], "cik": company["cik"],
            "accession_number": metadata["accession_number"], "source_url": metadata["source_url"],
        }
        numeric_rows.extend({**common, **item} for item in filing["numeric_review"])
        for query in filing["retrieval_review"]:
            for result in query["results"]:
                retrieval_rows.append({
                    **common, "query_id": query["query_id"], "query": query["query"],
                    **result, "reviewer_notes": query.get("reviewer_notes", ""),
                })
        summary_rows.append({
            **common, "form": metadata["form"], "filing_date": metadata["filing_date"],
            "xbrl_coverage_pct": filing["system_observation"]["xbrl_coverage_pct"],
            "missing_concepts": ",".join(filing["system_observation"]["missing_concepts"]),
            "numeric_checks": len(filing["numeric_review"]),
            "retrieval_queries": len(filing["retrieval_review"]),
        })
    _write_json(output / "review_queue.json", queue)
    _write_csv(output / "numeric_review.csv", numeric_rows, [
        "ticker", "cik", "accession_number", "source_url", "concept", "period", "system_value",
        "source", "citation", "verified_value", "verified", "reviewer_notes",
    ])
    _write_csv(output / "retrieval_review.csv", retrieval_rows, [
        "ticker", "cik", "accession_number", "source_url", "query_id", "query", "chunk_id",
        "section", "kind", "text", "relevant", "reviewer_notes",
    ])
    _write_csv(output / "filing_summary.csv", summary_rows, [
        "ticker", "cik", "accession_number", "source_url", "form", "filing_date",
        "xbrl_coverage_pct", "missing_concepts", "numeric_checks", "retrieval_queries",
    ])
    return {"numeric_checks": len(numeric_rows), "retrieval_results": len(retrieval_rows)}


def _retrieval_review(html: str, retrieval: RetrievalStack) -> list[dict]:
    chunks = chunk_blocks(parse_filing_html(html))
    if not chunks:
        return []
    index = HybridIndex(retrieval.embeddings)
    try:
        index.build(chunks)
        reviews = []
        for query_id, query in REVIEW_QUERIES:
            hits = retrieval.rerank(query, index.search(query), top_k=5)
            reviews.append({
                "query_id": query_id,
                "query": query,
                "reviewer_notes": "",
                "results": [{
                    "chunk_id": hit.chunk.chunk_id,
                    "section": hit.chunk.section,
                    "kind": hit.chunk.kind,
                    "text": hit.chunk.text,
                    "relevant": None,
                } for hit in hits],
            })
        return reviews
    finally:
        index.close()


def prepare_review_queue(
    watchlist: Watchlist,
    fetch_submissions: Callable[[str], dict],
    fetch_companyfacts: Callable[[str], dict],
    fetch_html: Callable[[FilingEvent], str],
    output_dir: Path | str,
    filings_per_company: int = 2,
    retrieval: RetrievalStack | None = None,
) -> dict:
    if filings_per_company < 1:
        raise ValueError("filings_per_company must be at least 1")
    retrieval = retrieval or build_retrieval_stack()
    output = Path(output_dir)
    filings = []
    errors = {}
    for company in watchlist.companies:
        try:
            submissions = fetch_submissions(company.cik)
            events = [event for event in parse_recent_filings(submissions, company.cik)
                      if event.form.replace("/A", "") in {"10-K", "10-Q"}]
            selected = sorted(events, key=lambda event: (event.filing_date, event.accession_number), reverse=True)[:filings_per_company]
            if not selected:
                raise ValueError("No recent 10-K/10-Q filings found")
            facts = fetch_companyfacts(company.cik)
            facts_path = output / "corpus" / company.cik / "companyfacts.json"
            _write_json(facts_path, facts)
        except Exception as exc:
            errors[company.cik] = str(exc)
            continue

        for event in selected:
            try:
                html = fetch_html(event)
                html_path = output / "corpus" / company.cik / f"{event.accession_number}.html"
                html_path.parent.mkdir(parents=True, exist_ok=True)
                html_path.write_text(html, encoding="utf-8")
                result = process_filing(event, lambda _: facts, lambda _: html,
                                        data_provenance_note="UNLABELED REAL SEC BENCHMARK DRAFT",
                                        retrieval=retrieval)
                memo_path = write_memo(result, output / "memos")
                filings.append({
                    "company": {"name": company.name, "ticker": company.ticker, "cik": company.cik},
                    "filing": {
                        "accession_number": event.accession_number,
                        "form": event.form,
                        "filing_date": event.filing_date,
                        "report_date": event.report_date,
                        "primary_document": event.primary_document,
                        "source_url": _source_url(event),
                        "companyfacts_path": facts_path.relative_to(output).as_posix(),
                        "html_path": html_path.relative_to(output).as_posix(),
                        "memo_path": memo_path.relative_to(output).as_posix(),
                    },
                    "system_observation": {
                        "xbrl_coverage_pct": result.coverage_pct,
                        "missing_concepts": [concept.value for concept in result.missing_concepts],
                        "latency_seconds": result.elapsed_seconds,
                    },
                    "numeric_review": _numeric_review(result.memo.metric_rows),
                    "retrieval_review": _retrieval_review(html, retrieval),
                })
            except Exception as exc:
                errors[event.accession_number] = str(exc)

    queue = {
        "version": 1,
        "status": "draft_unlabeled",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Which stack retrieved these passages. Relevance labels are only valid
        # for this stack, so compile and the harness carry it forward.
        "retrieval_stack": retrieval.fingerprint,
        "instructions": [
            "Open each filing source_url and independently verify numeric values before setting verified=true.",
            "For each retrieval result, set relevant=true or false; do not leave reviewed queries null.",
            "Add reviewer notes for ambiguous periods, units, tables, or narrative relevance.",
            "This draft is not ground truth and must not be used to claim PRD metric compliance.",
            "Labels apply to the recorded retrieval_stack only; changing the stack requires re-preparing and re-labeling.",
        ],
        "filings": filings,
        "errors": errors,
    }
    export_review_files(queue, output)
    return queue


def main() -> int:
    parser = argparse.ArgumentParser(description="Download real filings and create a manual-labeling queue")
    parser.add_argument("--watchlist", type=Path, default=ROOT / "config/watchlist.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/evaluation/real-draft")
    parser.add_argument("--filings-per-company", type=int, default=2)
    parser.add_argument("--export-existing", action="store_true", help="Regenerate CSV files from an existing review_queue.json")
    parser.add_argument("--force", action="store_true", help="Replace an existing unlabeled download queue")
    parser.add_argument("--retrieval-profile", choices=PROFILES,
                        help="Stack used to retrieve the passages for labeling. Recorded in the "
                             "queue; labels are only valid for the stack that produced them.")
    args = parser.parse_args()
    queue_path = args.output_dir / "review_queue.json"
    if args.export_existing:
        if not queue_path.is_file():
            parser.error(f"Review queue does not exist: {queue_path}")
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        counts = export_review_files(queue, args.output_dir)
        print(json.dumps({"review_queue": str(queue_path), **counts}, indent=2))
        return 0
    if queue_path.exists() and not args.force:
        parser.error(f"Review queue already exists: {queue_path}. Use --export-existing or --force.")
    load_dotenv(ROOT / ".env")
    client = SecXbrlClient(os.getenv("SEC_USER_AGENT", ""), cache_dir=str(args.output_dir / "xbrl-cache"))
    try:
        retrieval = build_retrieval_stack(args.retrieval_profile)
    except ValueError as exc:
        parser.error(str(exc))
    queue = prepare_review_queue(
        load_watchlist(args.watchlist), client.get_submissions,
        lambda cik: client.get_company_facts(cik, use_cache=True), client.get_filing_html,
        args.output_dir, args.filings_per_company, retrieval=retrieval,
    )
    print(json.dumps({
        "review_queue": str(args.output_dir / "review_queue.json"),
        "filings_prepared": len(queue["filings"]),
        "retrieval_queries_prepared": sum(len(f["retrieval_review"]) for f in queue["filings"]),
        "errors": queue["errors"],
    }, indent=2))
    return 1 if queue["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

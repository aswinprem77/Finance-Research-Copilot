"""Compile completed review spreadsheets into a real labeled benchmark."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re

from src.evaluation.models import Benchmark
from src.schema.financial_schema import FinancialConcept

ROOT = Path(__file__).resolve().parents[2]
PERIOD_RE = re.compile(r"^(Q[1-4]|FY) FY(\d{4})$")


def _rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _boolean(value: str, label: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{label} must be true or false, found {value!r}")


def _project_path(queue_dir: Path, relative: str, root: Path) -> str:
    resolved = (queue_dir / relative).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"Corpus path is outside the project root: {resolved}") from exc


def compile_review_queue(queue_dir: Path | str, project_root: Path | str,
                         *, require_prd_size: bool = True) -> Benchmark:
    queue_dir = Path(queue_dir)
    root = Path(project_root)
    queue = json.loads((queue_dir / "review_queue.json").read_text(encoding="utf-8"))
    numeric_rows = _rows(queue_dir / "numeric_review.csv")
    retrieval_rows = _rows(queue_dir / "retrieval_review.csv")
    numeric_index = {(r["accession_number"], r["concept"], r["period"]): r for r in numeric_rows}
    retrieval_index = {(r["accession_number"], r["query_id"], r["chunk_id"]): r for r in retrieval_rows}

    numeric_cases = []
    retrieval_cases = []
    pipeline_cases = []
    company_ciks = set()
    query_count = 0
    for item in queue["filings"]:
        company = item["company"]
        filing = item["filing"]
        accession = filing["accession_number"]
        company_ciks.add(company["cik"])
        expected_facts = []
        periods = set()
        for candidate in item["numeric_review"]:
            key = (accession, candidate["concept"], candidate["period"])
            reviewed = numeric_index.get(key)
            if reviewed is None:
                raise ValueError(f"Missing numeric review row: {key}")
            if not _boolean(reviewed["verified"], f"numeric {key} verified"):
                raise ValueError(f"Numeric row is not verified: {key}")
            if not reviewed["verified_value"].strip():
                raise ValueError(f"Numeric row has no independently verified value: {key}")
            try:
                verified_value = float(reviewed["verified_value"])
            except ValueError as exc:
                raise ValueError(f"Numeric row has an invalid verified value: {key}") from exc
            if not math.isfinite(verified_value):
                raise ValueError(f"Numeric row verified value must be finite: {key}")
            match = PERIOD_RE.fullmatch(candidate["period"])
            if not match:
                raise ValueError(f"Unsupported period label: {candidate['period']}")
            fiscal_period, fiscal_year = match.group(1), int(match.group(2))
            periods.add((fiscal_year, fiscal_period))
            if candidate["source"] in {"xbrl", "derived"}:
                expected_facts.append({
                    "concept": candidate["concept"], "fiscal_year": fiscal_year,
                    "fiscal_period": fiscal_period, "value": verified_value,
                    "absolute_tolerance": 0,
                })
        if len(periods) != 1:
            raise ValueError(f"Filing {accession} has multiple or missing current periods: {periods}")
        fiscal_year, fiscal_period = next(iter(periods))
        if not expected_facts:
            raise ValueError(f"Filing {accession} has no verified XBRL facts")
        numeric_cases.append({
            "case_id": f"{company['ticker'].lower()}-{accession}-numeric",
            "companyfacts_path": _project_path(queue_dir, filing["companyfacts_path"], root),
            "cik": company["cik"], "accession_number": accession,
            "filed_date": filing["filing_date"], "period_end_date": filing["report_date"],
            "fiscal_year": fiscal_year, "fiscal_period": fiscal_period,
            "required_concepts": [concept.value for concept in FinancialConcept],
            "expected_facts": expected_facts,
        })

        queries = []
        for query in item["retrieval_review"]:
            relevant = []
            for result in query["results"]:
                key = (accession, query["query_id"], result["chunk_id"])
                reviewed = retrieval_index.get(key)
                if reviewed is None:
                    raise ValueError(f"Missing retrieval review row: {key}")
                if _boolean(reviewed["relevant"], f"retrieval {key} relevant"):
                    relevant.append(result["chunk_id"])
            if not relevant:
                raise ValueError(f"Query {accession}/{query['query_id']} has no relevant labeled chunk")
            queries.append({
                "query_id": f"{company['ticker'].lower()}-{accession}-{query['query_id']}",
                "query": query["query"], "relevant_chunk_ids": relevant,
                "top_k": len(query["results"]),
            })
            query_count += 1
        retrieval_cases.append({
            "case_id": f"{company['ticker'].lower()}-{accession}-retrieval",
            "filing_html_path": _project_path(queue_dir, filing["html_path"], root),
            "queries": queries,
        })
        pipeline_cases.append({
            "case_id": f"{company['ticker'].lower()}-{accession}-pipeline",
            "companyfacts_path": _project_path(queue_dir, filing["companyfacts_path"], root),
            "filing_html_path": _project_path(queue_dir, filing["html_path"], root),
            "company_cik": company["cik"], "company_name": company["name"],
            "accession_number": accession, "form": filing["form"],
            "filing_date": filing["filing_date"], "report_date": filing["report_date"],
            "primary_document": filing["primary_document"],
        })

    if require_prd_size and (len(company_ciks) < 5 or query_count < 30):
        raise ValueError(f"PRD benchmark requires at least 5 companies and 30 queries; found {len(company_ciks)} and {query_count}")
    return Benchmark.model_validate({
        "benchmark_id": "real-watchlist-v1",
        "description": "Manually verified SEC watchlist benchmark compiled from review spreadsheets.",
        "scope": "real_labeled", "numeric_cases": numeric_cases,
        "retrieval_cases": retrieval_cases, "citation_cases": [], "pipeline_cases": pipeline_cases,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile completed review CSVs into a labeled benchmark")
    parser.add_argument("--queue-dir", type=Path, default=ROOT / "data/evaluation/real-draft")
    parser.add_argument("--output", type=Path, default=ROOT / "data/evaluation/real-watchlist-v1.json")
    args = parser.parse_args()
    try:
        benchmark = compile_review_queue(args.queue_dir, ROOT)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(benchmark.model_dump(mode="json"), indent=2), encoding="utf-8")
    print(json.dumps({
        "benchmark": str(args.output),
        "companies": len({case.cik for case in benchmark.numeric_cases}),
        "numeric_facts": sum(len(case.expected_facts) for case in benchmark.numeric_cases),
        "retrieval_queries": sum(len(case.queries) for case in benchmark.retrieval_cases),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

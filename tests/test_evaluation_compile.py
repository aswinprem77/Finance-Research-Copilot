import csv
import json
from pathlib import Path

import pytest

from src.evaluation.compile import compile_review_queue
from src.evaluation.prepare import prepare_review_queue
from src.pipeline.watchlist import Watchlist, WatchlistCompany

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"


def _prepare(tmp_path):
    watchlist = Watchlist("Synthetic", [WatchlistCompany("Test Co", "TEST", "0000320193")])
    submissions = lambda _: {"name": "Test", "filings": {"recent": {
        "accessionNumber": [ACCESSION], "form": ["10-Q"], "filingDate": ["2024-07-20"],
        "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"],
    }}}
    facts = lambda _: json.loads((FIXTURES / "sample_companyfacts.json").read_text(encoding="utf-8"))
    html = lambda _: (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")
    prepare_review_queue(watchlist, submissions, facts, html, tmp_path, 1)


def _edit_csv(path, callback):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = reader.fieldnames
    callback(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_compile_requires_completed_labels(tmp_path):
    _prepare(tmp_path)
    with pytest.raises(ValueError, match="must be true or false"):
        compile_review_queue(tmp_path, ROOT, require_prd_size=False)


ROOT = Path(__file__).resolve().parents[1]


def test_compiles_verified_csvs_into_real_benchmark(tmp_path):
    _prepare(tmp_path)
    _edit_csv(tmp_path / "numeric_review.csv", lambda rows: [row.update(
        verified="true", verified_value=row["system_value"]) for row in rows])
    def label_retrieval(rows):
        seen = set()
        for row in rows:
            key = row["query_id"]
            row["relevant"] = "true" if key not in seen else "false"
            seen.add(key)
    _edit_csv(tmp_path / "retrieval_review.csv", label_retrieval)
    benchmark = compile_review_queue(tmp_path, tmp_path, require_prd_size=False)
    assert benchmark.scope == "real_labeled"
    assert len(benchmark.numeric_cases) == 1
    assert len(benchmark.numeric_cases[0].expected_facts) == 2  # XBRL only; HTML fallback stays excluded
    assert len(benchmark.retrieval_cases[0].queries) == 5


def test_prd_size_gate_rejects_tiny_real_benchmark(tmp_path):
    _prepare(tmp_path)
    _edit_csv(tmp_path / "numeric_review.csv", lambda rows: [row.update(
        verified="true", verified_value=row["system_value"]) for row in rows])
    def label_retrieval(rows):
        seen = set()
        for row in rows:
            row["relevant"] = "true" if row["query_id"] not in seen else "false"
            seen.add(row["query_id"])
    _edit_csv(tmp_path / "retrieval_review.csv", label_retrieval)
    with pytest.raises(ValueError, match="at least 5 companies and 30 queries"):
        compile_review_queue(tmp_path, tmp_path)


def test_compile_rejects_non_finite_verified_values(tmp_path):
    _prepare(tmp_path)
    _edit_csv(tmp_path / "numeric_review.csv", lambda rows: [row.update(
        verified="true", verified_value="NaN") for row in rows])
    with pytest.raises(ValueError, match="must be finite"):
        compile_review_queue(tmp_path, tmp_path, require_prd_size=False)

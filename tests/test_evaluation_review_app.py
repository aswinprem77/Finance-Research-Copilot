import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.evaluation.prepare import prepare_review_queue
from src.evaluation.review_app import ReviewStore, make_server
from src.pipeline.watchlist import Watchlist, WatchlistCompany


FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"


def _prepare(queue_dir: Path) -> None:
    watchlist = Watchlist("Synthetic", [
        WatchlistCompany("Test Co", "TEST", "0000320193"),
    ])
    submissions = lambda _: {"name": "Test", "filings": {"recent": {
        "accessionNumber": [ACCESSION], "form": ["10-Q"],
        "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"],
        "primaryDocument": ["test.htm"],
    }}}
    facts = lambda _: json.loads(
        (FIXTURES / "sample_companyfacts.json").read_text(encoding="utf-8")
    )
    html = lambda _: (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")
    prepare_review_queue(watchlist, submissions, facts, html, queue_dir, 1)


def _json_request(url: str, method: str = "GET", payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), json.loads(response.read())


def test_review_store_validates_and_persists_labels(tmp_path):
    queue_dir = tmp_path / "queue"
    _prepare(queue_dir)
    store = ReviewStore(queue_dir, tmp_path, require_prd_size=False)
    state = store.state()
    assert state["progress"]["numeric_completed"] == 0
    assert state["progress"]["retrieval_completed"] == 0
    assert not state["progress"]["ready_to_compile"]

    filing = state["queue"]["filings"][0]
    numeric = filing["numeric_review"][0]
    assert isinstance(numeric["system_value"], str)
    numeric_key = {
        "accession_number": ACCESSION,
        "concept": numeric["concept"],
        "period": numeric["period"],
    }
    with pytest.raises(ValueError, match="finite"):
        store.update_numeric({**numeric_key, "verified": True, "verified_value": "NaN"})
    with pytest.raises(ValueError, match="true or null"):
        store.update_numeric({**numeric_key, "verified": False, "verified_value": "1"})

    store.update_numeric({
        **numeric_key,
        "verified": True,
        "verified_value": numeric["system_value"],
        "reviewer_notes": "Checked against the filing.",
    })
    query = filing["retrieval_review"][0]
    result = query["results"][0]
    store.update_retrieval({
        "accession_number": ACCESSION,
        "query_id": query["query_id"],
        "chunk_id": result["chunk_id"],
        "relevant": True,
        "reviewer_notes": "Directly answers the query.",
    })

    reloaded = ReviewStore(queue_dir, tmp_path, require_prd_size=False).state()
    saved = reloaded["queue"]["filings"][0]
    assert saved["numeric_review"][0]["verified"] is True
    assert saved["numeric_review"][0]["reviewer_notes"] == "Checked against the filing."
    assert saved["retrieval_review"][0]["results"][0]["relevant"] is True
    assert reloaded["progress"]["numeric_completed"] == 1
    assert reloaded["progress"]["retrieval_completed"] == 1


def test_completed_review_compiles_and_evaluates(tmp_path):
    queue_dir = tmp_path / "queue"
    _prepare(queue_dir)
    store = ReviewStore(queue_dir, tmp_path, require_prd_size=False)
    filing = store.state()["queue"]["filings"][0]

    for item in filing["numeric_review"]:
        store.update_numeric({
            "accession_number": ACCESSION,
            "concept": item["concept"],
            "period": item["period"],
            "verified": True,
            "verified_value": item["system_value"],
            "reviewer_notes": "Synthetic test review.",
        })
    for query in filing["retrieval_review"]:
        for rank, result in enumerate(query["results"]):
            store.update_retrieval({
                "accession_number": ACCESSION,
                "query_id": query["query_id"],
                "chunk_id": result["chunk_id"],
                "relevant": rank == 0,
                "reviewer_notes": "Synthetic test review.",
            })

    assert store.state()["progress"]["ready_to_compile"]
    output = store.compile_and_evaluate()
    assert Path(output["benchmark"]).is_file()
    assert Path(output["report_json"]).is_file()
    assert Path(output["report_markdown"]).is_file()
    assert output["report"]["benchmark_id"] == "real-watchlist-v1"


def test_local_http_interface_serves_state_and_updates(tmp_path):
    queue_dir = tmp_path / "queue"
    _prepare(queue_dir)
    store = ReviewStore(queue_dir, tmp_path, require_prd_size=False)
    server = make_server("127.0.0.1", 0, store)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert "Financial Research Benchmark Review" in page
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

        status, headers, state = _json_request(base + "/api/state")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        item = state["queue"]["filings"][0]["numeric_review"][0]
        status, _, update = _json_request(base + "/api/numeric", "PATCH", {
            "accession_number": ACCESSION,
            "concept": item["concept"],
            "period": item["period"],
            "verified": True,
            "verified_value": item["system_value"],
            "reviewer_notes": "HTTP save",
        })
        assert status == 200
        assert update["progress"]["numeric_completed"] == 1

        with pytest.raises(HTTPError) as incomplete:
            _json_request(base + "/api/compile", "POST", {})
        assert incomplete.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

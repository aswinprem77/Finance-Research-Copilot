"""
Tests for the browser UI shell.

Two things matter here and neither is about appearance. The shell must load
without a key, because a browser cannot attach one to a page navigation and
the page is what asks for it. And the shell must contain no filing data, or
making it public would hand out exactly what the key protects.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.pipeline.runner import run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.service.api import UI_PATH, create_app
from src.service.auth import HEADER_NAME

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"
KEY = "ui-test-key"


def submissions(cik):
    return {"filings": {"recent": {"accessionNumber": [ACCESSION], "form": ["10-Q"],
            "filingDate": ["2024-07-20"], "reportDate": ["2024-06-30"], "primaryDocument": ["test.htm"]}}}


def facts(cik):
    return json.loads((FIXTURES / "sample_companyfacts.json").read_text())


def html(filing):
    return (FIXTURES / "sample_filing_excerpt.html").read_text(encoding="utf-8")


@pytest.fixture
def populated(tmp_path):
    run_poll_cycle(Watchlist("t", [WatchlistCompany("SYNTHETIC TEST CO", "TEST", "0000320193")]),
                   submissions, facts, html, state_path=tmp_path / "completed.json",
                   output_dir=tmp_path / "memos", data_provenance_note="SYNTHETIC",
                   records_dir=tmp_path / "records")
    return tmp_path


def test_the_shell_loads_without_a_key(populated):
    # A page navigation cannot carry a custom header, and this page is what
    # asks for the key.
    client = TestClient(create_app(populated, api_keys={KEY}))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Financial Research Copilot" in response.text


def test_the_shell_carries_no_filing_data(populated):
    client = TestClient(create_app(populated, api_keys={KEY}))
    page = client.get("/").text
    # Nothing the key protects may appear in an unauthenticated response.
    assert ACCESSION not in page
    assert "SYNTHETIC TEST CO" not in page
    assert str(populated) not in page
    assert "0000320193" not in page


def test_the_shell_is_the_file_on_disk(populated):
    client = TestClient(create_app(populated, api_keys={KEY}))
    assert client.get("/").text == UI_PATH.read_text(encoding="utf-8")


def test_the_data_endpoints_the_page_calls_stay_protected(populated):
    client = TestClient(create_app(populated, api_keys={KEY}))
    for path in ("/health", "/watchlist", "/filings", f"/filings/{ACCESSION}"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={HEADER_NAME: KEY}).status_code == 200, path


def test_the_page_is_served_when_auth_is_off(populated):
    client = TestClient(create_app(populated, api_keys=set()))
    assert client.get("/").status_code == 200


def test_the_page_references_only_endpoints_that_exist(populated):
    app = create_app(populated, api_keys=set())
    page = UI_PATH.read_text(encoding="utf-8")
    routes = {getattr(r, "path", "") for r in app.routes}

    # Fetch targets written as string literals or template literals in the page.
    referenced = set(re.findall(r'api\(\s*[`"\']([^`"\'?]+)', page))
    referenced |= set(re.findall(r'fetch\(\s*[`"\']([^`"\'?]+)', page))
    assert referenced, "expected the page to call the API"

    for target in referenced:
        # Normalise the JS template placeholders to FastAPI's path parameters.
        candidate = re.sub(r"\$\{[^}]+\}", "{accession}", target).rstrip("?")
        assert candidate in routes, f"page calls {target!r}, which is not a route"

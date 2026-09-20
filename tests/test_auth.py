"""
Tests for API key authentication.

The behaviours worth guarding are the ones that fail quietly: that /docs and
/openapi.json are covered rather than only the data routes, that a wrong key
is not distinguishable from a missing one, and that the open-by-default state
cannot be combined with a public bind.
"""
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.pipeline.runner import run_poll_cycle
from src.pipeline.watchlist import Watchlist, WatchlistCompany
from src.service.api import create_app
from src.service.auth import (
    HEADER_NAME,
    describe,
    is_loopback,
    key_is_valid,
    load_api_keys,
    presented_key,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0000320193-24-000020"
KEY = "s3cret-key-value"


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


@pytest.fixture
def secured(populated):
    return TestClient(create_app(populated, api_keys={KEY}))


@pytest.fixture
def open_client(populated):
    return TestClient(create_app(populated, api_keys=set()))


# --- key configuration -------------------------------------------------------

def test_keys_are_read_comma_separated():
    assert load_api_keys({"COPILOT_API_KEYS": "a,b , c"}) == {"a", "b", "c"}


def test_blank_entries_never_become_valid_keys():
    # A trailing comma or an empty variable must not authorise an empty key.
    assert load_api_keys({"COPILOT_API_KEYS": ""}) == set()
    assert load_api_keys({"COPILOT_API_KEYS": " , ,"}) == set()
    assert load_api_keys({}) == set()
    assert not key_is_valid("", {"a"})
    assert not key_is_valid(None, {"a"})


def test_validation_accepts_any_configured_key():
    # Several keys so one can be rotated out without downtime.
    assert key_is_valid("second", {"first", "second"})
    assert not key_is_valid("third", {"first", "second"})


# --- header parsing ----------------------------------------------------------

def test_key_is_read_from_either_header():
    assert presented_key({HEADER_NAME: " abc "}) == "abc"
    assert presented_key({"Authorization": "Bearer abc"}) == "abc"
    assert presented_key({"Authorization": "bearer abc"}) == "abc"


def test_other_authorization_schemes_are_ignored():
    assert presented_key({"Authorization": "Basic abc"}) is None
    assert presented_key({"Authorization": "Bearer   "}) is None
    assert presented_key({}) is None


# --- loopback detection ------------------------------------------------------

def test_loopback_addresses_are_recognised():
    assert is_loopback("127.0.0.1")
    assert is_loopback("localhost")
    assert is_loopback("::1")


def test_every_interface_is_not_loopback():
    # These are the ones that would expose the service.
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("::")
    assert not is_loopback("")
    assert not is_loopback("192.168.1.10")


def test_a_hostname_is_treated_as_public():
    # It may resolve anywhere, and guessing wrong here would fail open.
    assert not is_loopback("copilot.internal")


# --- enforcement -------------------------------------------------------------

def test_requests_without_a_key_are_rejected(secured):
    response = secured.get("/filings")
    assert response.status_code == 401
    assert HEADER_NAME in response.headers["WWW-Authenticate"]


def test_a_valid_key_is_accepted_in_either_header(secured):
    assert secured.get("/filings", headers={HEADER_NAME: KEY}).status_code == 200
    assert secured.get("/filings", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_a_wrong_key_is_indistinguishable_from_a_missing_one(secured):
    missing = secured.get("/filings")
    wrong = secured.get("/filings", headers={HEADER_NAME: "not-the-key"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_every_data_route_is_protected(secured):
    for path in ("/health", "/watchlist", "/filings", f"/filings/{ACCESSION}",
                 f"/filings/{ACCESSION}/flags", f"/filings/{ACCESSION}/peers",
                 f"/filings/{ACCESSION}/memo.md", f"/filings/{ACCESSION}/memo.pdf"):
        assert secured.get(path).status_code == 401, path


def test_the_docs_and_schema_are_protected(secured):
    # They describe the shape of everything else, so middleware covers them
    # rather than relying on each route being decorated.
    assert secured.get("/docs").status_code == 401
    assert secured.get("/openapi.json").status_code == 401
    assert secured.get("/docs", headers={HEADER_NAME: KEY}).status_code == 200


def test_the_poll_refusal_still_requires_a_key(secured):
    assert secured.post("/poll").status_code == 401
    assert secured.post("/poll", headers={HEADER_NAME: KEY}).status_code == 501


def test_liveness_is_reachable_without_a_key(secured):
    # So a container or load balancer probe does not need the credential.
    response = secured.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_reveals_nothing_about_the_data(secured):
    body = secured.get("/livez").json()
    assert set(body) == {"status"}


def test_unauthenticated_health_would_have_leaked_state(secured):
    # /health names the data directory and counts filings, so unlike /livez it
    # is not safe to expose.
    authorised = secured.get("/health", headers={HEADER_NAME: KEY}).json()
    assert "data_dir" in authorised and "processed_filings" in authorised


# --- open mode ---------------------------------------------------------------

def test_with_no_keys_configured_the_api_is_open(open_client):
    assert open_client.get("/filings").status_code == 200
    assert open_client.get("/health").status_code == 200


def test_describe_states_the_posture_plainly():
    assert "API key required (2 key(s) configured)" in describe({"a", "b"}, "0.0.0.0")
    assert "DISABLED" in describe(set(), "127.0.0.1")

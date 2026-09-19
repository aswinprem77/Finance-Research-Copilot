"""
SEC XBRL companyfacts API client — Path A (structured), per PRD v2 Section 5, Stage 2.

This is intentionally a thin client: fetch raw JSON, do minimal validation, hand off to
xbrl_parser.py to turn it into FinancialFact objects. No parsing logic lives here.

Companyfacts, submissions and primary HTML share one request-start limiter.
Live NVIDIA smoke testing is recorded in PROGRESS.md; it is not a coverage benchmark.
"""

from __future__ import annotations

import time
import re
from threading import Lock
from urllib.parse import quote
from pathlib import Path
from typing import Optional

import requests

SEC_BASE_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# SEC's Fair Access policy: max ~10 requests/second, and REQUIRES a descriptive User-Agent
# identifying the requester (company/individual + contact email). Requests without one are
# rejected. Set this via env var, never hardcode a real one into source control.
MIN_REQUEST_INTERVAL_SECONDS = 0.11  # keeps us safely under 10 req/sec


class SecXbrlClient:
    def __init__(self, user_agent: str, cache_dir: Optional[str] = None, timeout: int = 15):
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC requires a descriptive User-Agent with a contact email, e.g. "
                "'YourName YourEmail@example.com'. Refusing to proceed without one — "
                "SEC will reject (and may temporarily block) requests that don't identify "
                "the requester."
            )
        self.user_agent = user_agent
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_time: float = 0.0
        self._request_lock = Lock()

    def _headers(self) -> dict:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}

    def _throttle(self) -> None:
        with self._request_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
            self._last_request_time = time.monotonic()

    def _get(self, url: str):
        self._throttle()
        response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        response.raise_for_status()
        return response

    def get_submissions(self, cik: str) -> dict:
        return self._get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").json()

    def get_filing_html(self, event) -> str:
        accession = event.accession_number
        document = event.primary_document
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not document or any(s in document for s in ("..", "/", "\\", ":", "?", "#")):
            raise ValueError("Invalid filing accession or primary document")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(event.company_cik)}/{accession.replace('-', '')}/{quote(document)}"
        return self._get(url).text

    def _cache_path(self, cik: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"CIK{cik}.json"

    def get_company_facts(self, cik: str, use_cache: bool = True) -> dict:
        """
        Fetch raw XBRL companyfacts JSON for a company.

        cik: SEC Central Index Key. Will be zero-padded to 10 digits per SEC's URL format.
        use_cache: if a cache_dir was configured and a cached file exists, use it instead of
                   hitting the network again — keeps you inside the rate limit during dev/testing.
        """
        cik_padded = str(int(cik)).zfill(10)
        cache_path = self._cache_path(cik_padded)

        if use_cache and cache_path and cache_path.exists():
            import json
            return json.loads(cache_path.read_text())

        url = SEC_BASE_URL.format(cik=cik_padded)
        resp = self._get(url)
        data = resp.json()

        if cache_path:
            cache_path.write_text(resp.text)

        return data

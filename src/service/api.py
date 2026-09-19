"""
Read API over processed filings - PRD v2 Section 7, "API: FastAPI".

Deliberately almost entirely read-only. The pipeline writes run records and
memos; this serves them. Making the API a second writer to the same JSON
files would break the invariant the README states plainly ("One process must
own each state file"), and a filesystem is not the place to discover that.
Polling therefore stays with the CLI until there is a real queue behind it -
see POST /poll below, which refuses rather than pretending.

Everything served comes from the run records directory. No endpoint recomputes
anything, calls SEC, or loads a model, so responses are fast and a request
cannot change what a memo said.

Scope limits worth stating, because an API implies more than this one does:

- No authentication. It binds to localhost by default and is intended to run
  behind something that does authenticate. Do not expose it as-is.
- No pagination beyond a `limit`. The watchlist produces a few filings a
  quarter; this is not a dataset that needs cursors yet.
- Reads are not transactional. A record being rewritten during a listing is
  skipped rather than locked against, which is the same tradeoff
  list_run_records already makes.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from src.pipeline.watchlist import load_watchlist
from src.service.pdf import memo_pdf_bytes
from src.service.records import RunRecord, list_run_records, load_run_record

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "live"


def _data_dir() -> Path:
    return Path(os.getenv("COPILOT_DATA_DIR", str(DEFAULT_DATA_DIR)))


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    """
    `data_dir` is injected rather than read from the environment at import
    time so tests can point at a temporary directory without mutating global
    state, the same reason the watchlist loader takes a path.
    """
    base = Path(data_dir) if data_dir is not None else _data_dir()
    records_dir = base / "records"
    memos_dir = base / "memos"

    app = FastAPI(
        title="Financial Research Copilot",
        description="Read API over processed SEC filing memos. Decision support, not investment advice.",
        version="1.0.0",
    )

    def _record_or_404(accession: str) -> RunRecord:
        try:
            record = load_run_record(records_dir, accession)
        except ValueError as exc:
            # A malformed accession is the caller's error; an unreadable record
            # is ours. Both are reported rather than silently returning empty.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail=f"No processed filing {accession}")
        return record

    @app.get("/health", tags=["service"])
    def health() -> dict:
        return {
            "status": "ok",
            "data_dir": str(base),
            "records_available": records_dir.is_dir(),
            "processed_filings": len(list_run_records(records_dir)),
        }

    @app.get("/watchlist", tags=["service"])
    def watchlist() -> dict:
        wl = load_watchlist()
        return {"sector": wl.sector,
                "companies": [{"name": c.name, "ticker": c.ticker, "cik": c.cik} for c in wl.companies]}

    @app.get("/filings", tags=["filings"])
    def filings(
        cik: str | None = Query(default=None, description="Filter to one company CIK"),
        form: str | None = Query(default=None, description="Filter to one form type, e.g. 10-Q"),
        notable_only: bool = Query(default=False, description="Only filings with at least one notable flag"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict:
        records = list_run_records(records_dir)
        if cik:
            records = [r for r in records if r.company_cik == cik]
        if form:
            records = [r for r in records if r.form.upper() == form.upper()]
        if notable_only:
            records = [r for r in records if r.notable_flag_count > 0]
        return {"count": len(records), "filings": [r.summary() for r in records[:limit]]}

    @app.get("/filings/{accession}", tags=["filings"])
    def filing(accession: str) -> dict:
        return _record_or_404(accession).to_dict()

    @app.get("/filings/{accession}/flags", tags=["filings"])
    def flags(
        accession: str,
        severity: str | None = Query(default=None, description="notable or routine"),
    ) -> dict:
        record = _record_or_404(accession)
        selected = record.flags
        if severity:
            selected = [f for f in selected if f.get("severity") == severity]
        return {"accession_number": accession, "count": len(selected), "flags": selected}

    @app.get("/filings/{accession}/peers", tags=["filings"])
    def peers(accession: str) -> dict:
        record = _record_or_404(accession)
        if record.peer_comparison is None:
            raise HTTPException(status_code=404,
                                detail=f"No peer comparison was produced for {accession}")
        return record.peer_comparison

    @app.get("/filings/{accession}/memo.md", response_class=PlainTextResponse, tags=["filings"])
    def memo_markdown(accession: str) -> str:
        record = _record_or_404(accession)
        path = memos_dir / (record.memo_filename or f"{accession}.md")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Memo file missing for {accession}")
        return path.read_text(encoding="utf-8")

    @app.get("/filings/{accession}/memo.pdf", tags=["filings"])
    def memo_pdf(accession: str) -> Response:
        record = _record_or_404(accession)
        try:
            pdf = memo_pdf_bytes(record)
        except RuntimeError as exc:
            # reportlab is an optional dependency; say so rather than 500.
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        return Response(
            content=pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{accession}.pdf"'},
        )

    @app.post("/poll", status_code=501, tags=["service"])
    def poll() -> dict:
        """
        Not implemented on purpose.

        A poll writes the completion state, the narrative baselines and the run
        records. Exactly one process may own those, and an API worker cannot
        guarantee it is that process. Triggering polls from here needs the
        durable queue this phase has not built, so it refuses instead of
        corrupting state under concurrent requests.
        """
        raise HTTPException(
            status_code=501,
            detail=("Polling is CLI-owned: run `python -m src.pipeline --once`. "
                    "An API-triggered poll needs a durable queue so that only one "
                    "worker writes completion state; that is not built yet."),
        )

    return app


app = create_app()

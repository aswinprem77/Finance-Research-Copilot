"""Local-only web interface for independent benchmark labeling."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import RLock
from urllib.parse import urlparse

from src.evaluation.compile import compile_review_queue
from src.evaluation.harness import evaluate_benchmark
from src.retrieval.providers import stack_from_fingerprint

ROOT = Path(__file__).resolve().parents[2]
STATIC_HTML = Path(__file__).with_name("review.html")
MAX_REQUEST_BYTES = 64 * 1024


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def _atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _optional_bool(value, label: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true, false, or null")


def _csv_bool(value: str) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


class ReviewStore:
    def __init__(self, queue_dir: Path | str, project_root: Path | str = ROOT,
                 *, require_prd_size: bool = True):
        self.queue_dir = Path(queue_dir).resolve()
        self.project_root = Path(project_root).resolve()
        self.queue_path = self.queue_dir / "review_queue.json"
        self.numeric_path = self.queue_dir / "numeric_review.csv"
        self.retrieval_path = self.queue_dir / "retrieval_review.csv"
        self.require_prd_size = require_prd_size
        for path in (self.queue_path, self.numeric_path, self.retrieval_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        self._lock = RLock()
        self._queue = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self._numeric_fields, self._numeric = _read_csv(self.numeric_path)
        self._retrieval_fields, self._retrieval = _read_csv(self.retrieval_path)
        self._validate_unique_keys()

    def _validate_unique_keys(self) -> None:
        numeric = [(r["accession_number"], r["concept"], r["period"]) for r in self._numeric]
        retrieval = [(r["accession_number"], r["query_id"], r["chunk_id"]) for r in self._retrieval]
        if len(numeric) != len(set(numeric)):
            raise ValueError("numeric_review.csv contains duplicate review keys")
        if len(retrieval) != len(set(retrieval)):
            raise ValueError("retrieval_review.csv contains duplicate review keys")

    @staticmethod
    def _numeric_key(row: dict) -> tuple[str, str, str]:
        return row["accession_number"], row["concept"], row["period"]

    @staticmethod
    def _retrieval_key(row: dict) -> tuple[str, str, str]:
        return row["accession_number"], row["query_id"], row["chunk_id"]

    def _progress(self) -> dict:
        numeric_complete = 0
        for row in self._numeric:
            try:
                value = float(row["verified_value"])
                valid_value = math.isfinite(value)
            except (TypeError, ValueError):
                valid_value = False
            if _csv_bool(row["verified"]) is True and valid_value:
                numeric_complete += 1

        retrieval_complete = sum(_csv_bool(row["relevant"]) is not None for row in self._retrieval)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in self._retrieval:
            grouped.setdefault((row["accession_number"], row["query_id"]), []).append(row)
        queries_without_relevant = [
            f"{accession}/{query_id}" for (accession, query_id), rows in grouped.items()
            if all(_csv_bool(row["relevant"]) is not True for row in rows)
        ]
        ready = (numeric_complete == len(self._numeric)
                 and retrieval_complete == len(self._retrieval)
                 and not queries_without_relevant)
        return {
            "numeric_completed": numeric_complete,
            "numeric_total": len(self._numeric),
            "retrieval_completed": retrieval_complete,
            "retrieval_total": len(self._retrieval),
            "queries_without_relevant": queries_without_relevant,
            "ready_to_compile": ready,
            # Shown in the header so a reviewer always knows which stack
            # retrieved the passages they are labeling.
            "retrieval_stack": self._queue.get("retrieval_stack") or "unrecorded",
        }

    def state(self) -> dict:
        with self._lock:
            # JSON round-trip makes an isolated, serializable copy.
            queue = json.loads(json.dumps(self._queue))
            numeric = {self._numeric_key(row): row for row in self._numeric}
            retrieval = {self._retrieval_key(row): row for row in self._retrieval}
            for filing in queue["filings"]:
                accession = filing["filing"]["accession_number"]
                for item in filing["numeric_review"]:
                    row = numeric[(accession, item["concept"], item["period"])]
                    # Preserve the exact CSV representation for large SEC values.
                    item["system_value"] = row["system_value"]
                    item["verified_value"] = row["verified_value"] or None
                    item["verified"] = _csv_bool(row["verified"])
                    item["reviewer_notes"] = row["reviewer_notes"]
                for query in filing["retrieval_review"]:
                    for result in query["results"]:
                        row = retrieval[(accession, query["query_id"], result["chunk_id"])]
                        result["relevant"] = _csv_bool(row["relevant"])
                        result["reviewer_notes"] = row["reviewer_notes"]
            return {"queue": queue, "progress": self._progress()}

    def update_numeric(self, payload: dict) -> dict:
        required = ("accession_number", "concept", "period")
        if any(not isinstance(payload.get(field), str) for field in required):
            raise ValueError("Numeric update requires accession_number, concept, and period")
        verified = _optional_bool(payload.get("verified"), "verified")
        if verified is False:
            raise ValueError("verified must be true or null")
        value = str(payload.get("verified_value", "")).strip()
        notes = str(payload.get("reviewer_notes", ""))[:5000]
        if verified is True:
            try:
                if not math.isfinite(float(value)):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("A finite independently verified value is required") from exc
        key = tuple(payload[field] for field in required)
        with self._lock:
            row = next((row for row in self._numeric if self._numeric_key(row) == key), None)
            if row is None:
                raise KeyError(f"Unknown numeric review key: {key}")
            row["verified_value"] = value
            row["verified"] = "" if verified is None else str(verified).lower()
            row["reviewer_notes"] = notes
            _atomic_csv(self.numeric_path, self._numeric_fields, self._numeric)
            return self._progress()

    def update_retrieval(self, payload: dict) -> dict:
        required = ("accession_number", "query_id", "chunk_id")
        if any(not isinstance(payload.get(field), str) for field in required):
            raise ValueError("Retrieval update requires accession_number, query_id, and chunk_id")
        relevant = _optional_bool(payload.get("relevant"), "relevant")
        notes = str(payload.get("reviewer_notes", ""))[:5000]
        key = tuple(payload[field] for field in required)
        with self._lock:
            row = next((row for row in self._retrieval if self._retrieval_key(row) == key), None)
            if row is None:
                raise KeyError(f"Unknown retrieval review key: {key}")
            row["relevant"] = "" if relevant is None else str(relevant).lower()
            row["reviewer_notes"] = notes
            _atomic_csv(self.retrieval_path, self._retrieval_fields, self._retrieval)
            return self._progress()

    def compile_and_evaluate(self) -> dict:
        with self._lock:
            progress = self._progress()
            if not progress["ready_to_compile"]:
                raise ValueError("Review is incomplete or at least one query has no relevant passage")
            benchmark = compile_review_queue(
                self.queue_dir, self.project_root, require_prd_size=self.require_prd_size,
            )
            # Evaluate under the stack that retrieved these passages, whatever the
            # environment currently selects - the labels describe that stack's top-k.
            report = evaluate_benchmark(benchmark, self.project_root,
                                        retrieval=stack_from_fingerprint(benchmark.retrieval_stack))
            output_dir = self.queue_dir.parent
            benchmark_path = output_dir / "real-watchlist-v1.json"
            report_json_path = output_dir / "real-watchlist-v1-report.json"
            report_md_path = output_dir / "real-watchlist-v1-report.md"
            benchmark_path.write_text(json.dumps(benchmark.model_dump(mode="json"), indent=2), encoding="utf-8")
            report_json_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
            report_md_path.write_text(report.to_markdown(), encoding="utf-8")
            return {
                "benchmark": str(benchmark_path), "report_json": str(report_json_path),
                "report_markdown": str(report_md_path), "report": report.model_dump(mode="json"),
            }


def make_server(host: str, port: int, store: ReviewStore) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FRCReview/1.0"

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request body is empty or too large")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                body = STATIC_HTML.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(HTTPStatus.OK, store.state())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_PATCH(self):
            path = urlparse(self.path).path
            try:
                payload = self._payload()
                if path == "/api/numeric":
                    progress = store.update_numeric(payload)
                elif path == "/api/retrieval":
                    progress = store.update_retrieval(payload)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "progress": progress})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self):
            if urlparse(self.path).path != "/api/compile":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            try:
                self._json(HTTPStatus.OK, store.compile_and_evaluate())
            except (FileNotFoundError, ValueError) as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc), "progress": store.state()["progress"]})

        def log_message(self, format, *args):
            print(f"{self.client_address[0]} - {format % args}")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local benchmark review interface")
    parser.add_argument("--queue-dir", type=Path, default=ROOT / "data/evaluation/real-draft")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    store = ReviewStore(args.queue_dir, ROOT)
    host = "127.0.0.1"
    server = make_server(host, args.port, store)
    print(f"Review interface: http://{host}:{args.port}")
    print(f"Queue: {args.queue_dir.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping review interface.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Tracks which filing accession numbers have already been processed, so
repeated polling only triggers on genuinely NEW filings. File-backed JSON
so state survives across runs -- a Trigger agent that forgets what it's
seen on every restart would re-fire on every filing it already processed
last time it ran, defeating the entire point of "trigger on new".
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_PATH = Path(__file__).parent.parent.parent / "data" / "trigger_state.json"


def load_seen_accessions(path: Path | str = DEFAULT_STATE_PATH) -> set[str]:
    path = Path(path)
    if not path.exists():
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return set(json.load(fh))


def save_seen_accessions(accessions: set[str], path: Path | str = DEFAULT_STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sorted(accessions), fh, indent=2)

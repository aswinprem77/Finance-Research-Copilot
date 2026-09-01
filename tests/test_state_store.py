import tempfile
from pathlib import Path

from src.trigger.state_store import load_seen_accessions, save_seen_accessions


def test_load_returns_empty_set_when_file_does_not_exist():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "does_not_exist.json"
        assert load_seen_accessions(path) == set()


def test_save_then_load_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        save_seen_accessions({"acc-1", "acc-2"}, path)
        assert load_seen_accessions(path) == {"acc-1", "acc-2"}


def test_save_creates_parent_directories():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "dir" / "state.json"
        save_seen_accessions({"acc-1"}, path)
        assert path.exists()
        assert load_seen_accessions(path) == {"acc-1"}


def test_save_overwrites_not_appends():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        save_seen_accessions({"acc-1", "acc-2"}, path)
        save_seen_accessions({"acc-3"}, path)
        assert load_seen_accessions(path) == {"acc-3"}

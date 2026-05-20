"""Integration test: indexer.main() builds search.db from a real data/ tree."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from slack_log import indexer


@pytest.fixture
def tiny_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    cdir = data / "channels" / "C001" / "threads"
    cdir.mkdir(parents=True)
    (cdir / "1700000000.000001.jsonl").write_text(
        json.dumps({"ts": "1700000000.000001", "user": "U001", "text": "ping"}) + "\n"
    )
    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "display_name": "alice", "real_name": "Alice"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_channel": True},
    }))
    return data


def test_main_builds_index(tiny_data: Path, tmp_path: Path, monkeypatch):
    db = tmp_path / "search.db"
    monkeypatch.setattr(sys, "argv", [
        "indexer.py", "--data", str(tiny_data), "--db", str(db),
    ])
    indexer.main()

    assert db.exists()
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT count(*) FROM messages WHERE text MATCH 'ping'").fetchone()[0]
    conn.close()
    assert n == 1

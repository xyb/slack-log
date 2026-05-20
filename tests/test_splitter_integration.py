"""Integration tests: full splitter pipeline against a tiny SQLite fixture."""

import json
import sqlite3
from pathlib import Path

from slack_log import splitter


def test_full_pipeline_writes_users_channels_threads_and_index(
    sqlite_with_threads: Path, tmp_path: Path
):
    out = tmp_path / "data"
    out.mkdir()
    conn = sqlite3.connect(sqlite_with_threads)

    splitter.write_users_and_channels(conn, out)
    splitter.split_threads(conn, out)
    splitter.write_index(conn, out)
    conn.close()

    # 1) users.json populated with Alice
    users = json.loads((out / "users.json").read_text())
    assert "U001" in users
    assert users["U001"]["real_name"] == "Alice"

    # 2) channels.json contains both channels with their names
    channels = json.loads((out / "channels.json").read_text())
    assert channels["C001"]["name"] == "general"
    assert channels["C002"]["name"] == "random"

    # 3) thread jsonl files exist for valid threads
    c001_threads = sorted(p.name for p in (out / "channels" / "C001" / "threads").glob("*.jsonl"))
    assert c001_threads == ["1700000000.000001.jsonl", "1700000100.000002.jsonl"]
    # the thread parent file has the parent + 1 reply
    thread_lines = (out / "channels" / "C001" / "threads" / "1700000100.000002.jsonl").read_text().splitlines()
    assert len(thread_lines) == 2
    assert json.loads(thread_lines[0])["text"] == "parent thread"
    assert json.loads(thread_lines[1])["text"] == "reply 1"

    # 4) index.jsonl has one entry per thread anchor, with mention-resolved preview
    c001_index = (out / "channels" / "C001" / "index.jsonl").read_text().splitlines()
    assert len(c001_index) == 2
    entries = [json.loads(l) for l in c001_index]
    # the thread parent entry should report 1 reply
    thread_entry = next(e for e in entries if e["thread_ts"] == "1700000100.000002")
    assert thread_entry["msg_count"] == 2
    assert thread_entry["is_thread"] is True
    assert thread_entry["first_user"] == "U001"

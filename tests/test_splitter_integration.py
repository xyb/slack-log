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

    splitter.split(conn, out)
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
    entries = [json.loads(line) for line in c001_index]
    # the thread parent entry should report 1 reply
    thread_entry = next(e for e in entries if e["thread_ts"] == "1700000100.000002")
    assert thread_entry["msg_count"] == 2
    assert thread_entry["is_thread"] is True
    assert thread_entry["first_user"] == "U001"


def test_jsonl_sorted_when_table_order_is_not(tmp_path: Path):
    """Pass 3 (tidy): even when MESSAGE rows sit in the table newest-first,
    the thread jsonl comes out ts-ascending."""
    db = tmp_path / "s.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE MESSAGE (
            ID INTEGER, CHUNK_ID INTEGER,
            LOAD_DTTM TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHANNEL_ID TEXT, TS TEXT, THREAD_TS TEXT, LATEST_REPLY TEXT,
            IS_PARENT SMALLINT DEFAULT 0, DATA BLOB,
            PRIMARY KEY (ID, CHUNK_ID));
        CREATE TABLE S_USER (ID TEXT, CHUNK_ID INTEGER, DATA BLOB,
            PRIMARY KEY (ID, CHUNK_ID));
        CREATE TABLE CHANNEL (ID TEXT, CHUNK_ID INTEGER, DATA BLOB,
            PRIMARY KEY (ID, CHUNK_ID));
        """
    )
    rows = [  # inserted reverse-chronological — table order is NOT ts order
        ("100.000003", "reply 2", 0),
        ("100.000002", "reply 1", 0),
        ("100.000001", "parent", 1),
    ]
    for ts, txt, parent in rows:
        conn.execute(
            "INSERT INTO MESSAGE (ID, CHUNK_ID, CHANNEL_ID, TS, THREAD_TS, IS_PARENT, DATA) "
            "VALUES (?, 1, 'C1', ?, '100.000001', ?, ?)",
            (int(float(ts) * 1e6), ts, parent, json.dumps({"ts": ts, "text": txt}).encode()),
        )
    conn.commit()

    out = tmp_path / "data"
    splitter.split(conn, out)
    conn.close()

    lines = (out / "channels" / "C1" / "threads" / "100.000001.jsonl").read_text().splitlines()
    order = [json.loads(ln)["ts"] for ln in lines]
    assert order == sorted(order), f"jsonl not ts-sorted: {order}"
    assert json.loads(lines[0])["text"] == "parent"

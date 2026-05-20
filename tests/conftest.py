"""Shared test fixtures: minimal slackdump-like SQLite + sample JSONL data."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Make slack_log package importable without `pip install -e .`
sys.path.insert(0, str(Path(__file__).parent.parent))


def _schema(conn: sqlite3.Connection) -> None:
    """Subset of slackdump's schema — only what splitter actually queries."""
    conn.executescript(
        """
        CREATE TABLE CHUNK (
            ID INTEGER PRIMARY KEY,
            TYPE_ID SMALLINT NOT NULL DEFAULT 0
        );
        CREATE TABLE MESSAGE (
            ID INTEGER NOT NULL,
            CHUNK_ID INTEGER NOT NULL,
            LOAD_DTTM TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHANNEL_ID TEXT NOT NULL,
            TS TEXT NOT NULL,
            THREAD_TS TEXT,
            LATEST_REPLY TEXT,
            IS_PARENT SMALLINT NOT NULL DEFAULT 0,
            IDX INTEGER NOT NULL DEFAULT 0,
            NUM_FILES INTEGER NOT NULL DEFAULT 0,
            TXT TEXT,
            DATA BLOB NOT NULL,
            PRIMARY KEY (ID, CHUNK_ID)
        );
        CREATE TABLE S_USER (
            ID TEXT NOT NULL,
            CHUNK_ID INTEGER NOT NULL,
            LOAD_DTTM TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            IDX INTEGER NOT NULL DEFAULT 0,
            USERNAME TEXT NOT NULL,
            DATA BLOB NOT NULL,
            PRIMARY KEY (ID, CHUNK_ID)
        );
        CREATE TABLE CHANNEL (
            ID TEXT NOT NULL,
            CHUNK_ID INTEGER NOT NULL,
            LOAD_DTTM TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            NAME TEXT,
            IDX INTEGER NOT NULL DEFAULT 0,
            DATA BLOB NOT NULL,
            PRIMARY KEY (ID, CHUNK_ID)
        );
        INSERT INTO CHUNK (ID, TYPE_ID) VALUES (1, 0);
        """
    )


def _insert_msg(conn, ts: str, channel_id: str, thread_ts: str | None, text: str,
                user: str = "U001", data: dict | None = None, raw_data: bytes | None = None,
                is_parent: bool = False, latest_reply: str | None = None) -> None:
    if raw_data is not None:
        blob = raw_data
    else:
        msg = data if data is not None else {
            "type": "message", "user": user, "text": text, "ts": ts,
        }
        if thread_ts:
            msg["thread_ts"] = thread_ts
        blob = json.dumps(msg).encode()
    msg_id = int(float(ts) * 1_000_000)
    conn.execute(
        "INSERT INTO MESSAGE (ID, CHUNK_ID, CHANNEL_ID, TS, THREAD_TS, IS_PARENT, LATEST_REPLY, TXT, DATA) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, channel_id, ts, thread_ts, 1 if is_parent else 0, latest_reply, text, blob),
    )


@pytest.fixture
def sqlite_with_threads(tmp_path: Path) -> Path:
    """SQLite with 2 channels and 3 threads total (1 has invalid JSON in DATA)."""
    db = tmp_path / "slackdump.sqlite"
    conn = sqlite3.connect(db)
    _schema(conn)

    _insert_msg(conn, "1700000000.000001", "C001", None, "hello")
    _insert_msg(conn, "1700000100.000002", "C001", "1700000100.000002", "parent thread",
                is_parent=True, latest_reply="1700000101.000003")
    _insert_msg(conn, "1700000101.000003", "C001", "1700000100.000002", "reply 1")

    _insert_msg(conn, "1700000200.000004", "C002", None, "good message")
    _insert_msg(conn, "1700000300.000005", "C002", None, "broken",
                raw_data=b"not-valid-json{{{")

    conn.execute(
        "INSERT INTO CHANNEL (ID, CHUNK_ID, NAME, DATA) VALUES (?, 1, ?, ?)",
        ("C001", "general", json.dumps({"id": "C001", "name": "general"}).encode()),
    )
    conn.execute(
        "INSERT INTO CHANNEL (ID, CHUNK_ID, NAME, DATA) VALUES (?, 1, ?, ?)",
        ("C002", "random", json.dumps({"id": "C002", "name": "random"}).encode()),
    )
    conn.execute(
        "INSERT INTO S_USER (ID, CHUNK_ID, USERNAME, DATA) VALUES (?, 1, ?, ?)",
        ("U001", "alice", json.dumps({"id": "U001", "name": "alice", "real_name": "Alice"}).encode()),
    )

    conn.commit()
    conn.close()
    return db

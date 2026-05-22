"""Team profile: build search.db straight from slackdump.sqlite.

build_index(slackdump.sqlite, profile="team") is an ETL — no jsonl in between.
It fills the messages FTS table plus the materialized message_raw / threads /
channels / users tables the SqliteStore reads.

Uses the shared `sqlite_with_threads` fixture (conftest): 2 channels, 3 valid
thread anchors, 1 message with a corrupt DATA blob.
"""

import json
import sqlite3
from pathlib import Path

from slack_log import indexer


# --- iter_messages_from_sqlite --------------------------------------------

def test_iter_messages_from_sqlite_shape(sqlite_with_threads: Path):
    rows = list(indexer.iter_messages_from_sqlite(sqlite_with_threads))
    by_ts = {r["ts"]: r for r in rows}
    # 4 valid messages; the corrupt-DATA row is dropped.
    assert set(by_ts) == {
        "1700000000.000001", "1700000100.000002",
        "1700000101.000003", "1700000200.000004",
    }
    r = by_ts["1700000000.000001"]
    assert r["channel_id"] == "C001"
    assert r["channel_name"] == "general"
    assert r["kind"] == "channel"
    assert r["user_name"] == "Alice"  # no display_name → resolve_author uses real_name
    # a reply carries its parent's thread_ts
    assert by_ts["1700000101.000003"]["thread_ts"] == "1700000100.000002"


# --- build_index(profile="team") ------------------------------------------

def test_team_build_fills_messages_fts(sqlite_with_threads: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    stats = indexer.build_index(sqlite_with_threads, db, profile="team")
    assert stats["indexed"] == 4  # corrupt row excluded

    conn = indexer.open_db(db)
    hits = indexer.search(conn, "parent")
    conn.close()
    assert any("parent" in h["text"] for h in hits)


def test_team_build_fills_message_raw(sqlite_with_threads: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    indexer.build_index(sqlite_with_threads, db, profile="team")
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT channel_id, ts, thread_ts, data FROM message_raw ORDER BY ts"
    ).fetchall()
    conn.close()
    assert len(rows) == 4  # corrupt row excluded
    # each data blob is the complete message JSON
    parent = next(r for r in rows if r[1] == "1700000100.000002")
    msg = json.loads(parent[3])
    assert msg["text"] == "parent thread"
    assert parent[2] == "1700000100.000002"  # thread_ts == own ts for a parent
    # a reply's thread_ts points at the parent anchor
    reply = next(r for r in rows if r[1] == "1700000101.000003")
    assert reply[2] == "1700000100.000002"


def test_team_build_fills_threads(sqlite_with_threads: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    indexer.build_index(sqlite_with_threads, db, profile="team")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {(r["channel_id"], r["thread_ts"]): r
            for r in conn.execute("SELECT * FROM threads")}
    conn.close()
    # 3 valid anchors; the all-corrupt thread produces no row.
    assert set(rows) == {
        ("C001", "1700000000.000001"),
        ("C001", "1700000100.000002"),
        ("C002", "1700000200.000004"),
    }
    parent = rows[("C001", "1700000100.000002")]
    assert parent["msg_count"] == 2          # parent + 1 reply
    assert parent["is_thread"] == 1          # IS_PARENT row
    assert parent["first_text_preview"] == "parent thread"
    assert parent["latest_reply_ts"] == "1700000101.000003"
    assert parent["first_author_display"] == "Alice"
    standalone = rows[("C001", "1700000000.000001")]
    assert standalone["msg_count"] == 1
    assert standalone["is_thread"] == 0
    # no recorded latest reply → falls back to first_ts
    assert standalone["latest_reply_ts"] == "1700000000.000001"


def test_team_build_fills_channels_and_users(sqlite_with_threads: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    indexer.build_index(sqlite_with_threads, db, profile="team")
    conn = sqlite3.connect(db)
    chans = {r[0]: r for r in conn.execute(
        "SELECT id, name, kind, thread_count FROM channels")}
    users = {r[0]: r[1] for r in conn.execute("SELECT id, profile FROM users")}
    conn.close()
    assert chans["C001"][1] == "general"
    assert chans["C001"][2] == "channel"
    assert chans["C001"][3] == 2  # 2 thread anchors
    assert chans["C002"][3] == 1
    assert "U001" in users
    assert json.loads(users["U001"])["real_name"] == "Alice"


def test_team_build_include_filter(sqlite_with_threads: Path, tmp_path: Path):
    """include={'dm'} drops every channel — both fixture channels are real."""
    db = tmp_path / "search.db"
    stats = indexer.build_index(sqlite_with_threads, db, include={"dm"}, profile="team")
    assert stats["indexed"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM channels").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM message_raw").fetchone()[0] == 0
    conn.close()


def test_team_and_personal_index_same_messages(sqlite_with_threads: Path, tmp_path: Path):
    """The team ETL and the personal jsonl path index the same message set."""
    from slack_log import splitter

    data = tmp_path / "data"
    conn = sqlite3.connect(sqlite_with_threads)
    splitter.split(conn, data)
    conn.close()

    personal_db = tmp_path / "personal.db"
    team_db = tmp_path / "team.db"
    p = indexer.build_index(data, personal_db, profile="personal")
    t = indexer.build_index(sqlite_with_threads, team_db, profile="team")
    assert p["indexed"] == t["indexed"] == 4

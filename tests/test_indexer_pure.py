"""Unit tests for slack_log.pipeline.index — FTS5 build + search.

Indexer reads thread JSONL files produced by splitter (data/channels/<cid>/threads/*.jsonl)
plus users.json + channels.json, normalizes each message (resolves @mention uids to display
names, expands :shortcode: emoji, strips Slack <...> link wrappers), and writes them into
a standalone SQLite FTS5 database designed for v0.7 search.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from slack_log.pipeline import index


@pytest.fixture
def minimal_data(tmp_path: Path) -> Path:
    """A small data/ tree with two channels covering en/zh text, mention, emoji."""
    data = tmp_path / "data"
    c1 = data / "channels" / "C001" / "threads"
    c2 = data / "channels" / "C002" / "threads"
    c1.mkdir(parents=True)
    c2.mkdir(parents=True)

    (c1 / "1700000000.000001.jsonl").write_text(
        json.dumps({
            "ts": "1700000000.000001", "user": "U001",
            "text": "hello slackdump and the search index :heart:",
        }) + "\n" +
        json.dumps({
            "ts": "1700000010.000002", "user": "U002",
            "thread_ts": "1700000000.000001",
            "text": "reply from <@U001> 关于检索的反馈",
        }) + "\n"
    )
    (c2 / "1700000200.000003.jsonl").write_text(
        json.dumps({
            "ts": "1700000200.000003", "user": "U001",
            "text": "今天晚上发布新版本，注意监控",
        }) + "\n"
    )
    # one corrupt line — must not abort the build
    (c2 / "1700000300.000004.jsonl").write_text("not-valid-json{{{\n")

    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "real_name": "Alice", "display_name": "alice"},
        "U002": {"name": "bob", "real_name": "Bob", "display_name": "bob"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_channel": True},
        "C002": {"name": "ops", "is_channel": True},
    }))
    return data


def test_open_db_creates_schema(tmp_path: Path):
    db = tmp_path / "search.db"
    conn = index.open_db(db)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
    )
    assert cur.fetchone() is not None
    conn.close()


def test_iter_messages_yields_normalized_records(minimal_data: Path):
    rows = list(index.iter_messages(minimal_data))
    by_ts = {r["ts"]: r for r in rows}

    # Corrupt jsonl line silently skipped, valid records present.
    assert "1700000000.000001" in by_ts
    assert "1700000010.000002" in by_ts
    assert "1700000200.000003" in by_ts

    r1 = by_ts["1700000000.000001"]
    assert r1["channel_id"] == "C001"
    assert r1["channel_name"] == "general"
    assert r1["user_name"] == "alice"

    # :heart: shortcode expanded so users can search by either form.
    assert "❤" in r1["text"] or "heart" in r1["text"].lower()


def test_iter_messages_resolves_mentions(minimal_data: Path):
    rows = {r["ts"]: r for r in index.iter_messages(minimal_data)}
    reply = rows["1700000010.000002"]
    # <@U001> must resolve to display name so a search for "alice" hits the reply.
    assert "alice" in reply["text"].lower()
    assert "<@U001>" not in reply["text"]


def test_search_returns_user_id_for_uid_filtering(minimal_data: Path, tmp_path: Path):
    """user_id must be stored so /user/<uid> can filter precisely (handles homonyms)."""
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT user_id, user_name FROM messages WHERE user_id != ''").fetchall()
    conn.close()
    assert ("U001", "alice") in rows
    assert ("U002", "bob") in rows


def test_build_index_populates_fts(minimal_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    stats = index.build_index(minimal_data, db)
    assert stats["indexed"] >= 3

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert count >= 3
    conn.close()


def test_search_returns_english_hits(minimal_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = index.open_db(db)
    hits = index.search(conn, "slackdump")
    assert len(hits) >= 1
    assert any("slackdump" in h["text"].lower() for h in hits)
    # Hit metadata is enough to deep-link to the existing static HTML.
    h = hits[0]
    assert h["channel_id"] == "C001"
    assert h["thread_ts"] == "1700000000.000001"
    assert h["ts"] == "1700000000.000001"
    conn.close()


def test_search_matches_chinese_text(minimal_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = index.open_db(db)
    # FTS5 unicode61 tokenizer splits CJK char-by-char; querying any single
    # char (or short phrase) inside a Chinese sentence should still hit.
    hits = index.search(conn, "发布")
    assert len(hits) >= 1
    assert any("发布" in h["text"] for h in hits)
    conn.close()


def test_search_returns_snippet_with_marks(minimal_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = index.open_db(db)
    hits = index.search(conn, "slackdump")
    snippet = hits[0]["snippet"]
    # Snippet must mark the matched term so the HTML renderer can highlight it.
    assert "<mark>" in snippet and "</mark>" in snippet
    conn.close()


@pytest.mark.parametrize("q", [
    "@alice",                # bare @ — FTS5 column / mention syntax
    "@Xie Yanbo",            # @ + space + word
    "AND",                   # FTS5 boolean operator
    "OR",
    "NOT",
    "NEAR",
    "alice@example.com",     # email with @ and .
    "text:foo",              # FTS5 column-filter syntax
    '"unclosed',             # broken quote
    "(parens)",
    "term*wildcard",
    "^prefix",
])
def test_search_does_not_crash_on_special_syntax(minimal_data: Path, tmp_path: Path, q: str):
    """FTS5 query operators must never reach the engine raw — bare input would 500."""
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = index.open_db(db)
    # Either zero hits or some hits — never an OperationalError.
    index.search(conn, q)
    conn.close()


def test_search_at_mention_matches_user(minimal_data: Path, tmp_path: Path):
    """`@alice` should still find messages that mention alice."""
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    conn = index.open_db(db)
    hits = index.search(conn, "@alice")
    assert len(hits) >= 1
    conn.close()


@pytest.fixture
def mixed_kinds_data(tmp_path: Path) -> Path:
    """One real channel + one DM + one MPIM, each with one message."""
    data = tmp_path / "data"
    for cid in ("C001", "D001", "G001"):
        (data / "channels" / cid / "threads").mkdir(parents=True)
    (data / "channels" / "C001" / "threads" / "1.jsonl").write_text(
        json.dumps({"ts": "1.0", "user": "U001", "text": "channel msg"}) + "\n"
    )
    (data / "channels" / "D001" / "threads" / "2.jsonl").write_text(
        json.dumps({"ts": "2.0", "user": "U001", "text": "dm msg"}) + "\n"
    )
    (data / "channels" / "G001" / "threads" / "3.jsonl").write_text(
        json.dumps({"ts": "3.0", "user": "U001", "text": "mpim msg"}) + "\n"
    )
    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "display_name": "alice", "real_name": "Alice"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_channel": True},
        "D001": {"name": "bob-dm", "is_im": True},
        "G001": {"name": "mpdm", "is_mpim": True},
    }))
    return data


def test_iter_messages_attaches_kind(mixed_kinds_data: Path):
    rows = list(index.iter_messages(mixed_kinds_data))
    by_cid = {r["channel_id"]: r["kind"] for r in rows}
    assert by_cid["C001"] == "channel"
    assert by_cid["D001"] == "dm"
    assert by_cid["G001"] == "mpim"


def test_build_index_include_channel_only(mixed_kinds_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(mixed_kinds_data, db, include={"channel"})
    conn = sqlite3.connect(db)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM messages").fetchall()}
    conn.close()
    assert kinds == {"channel"}


def test_build_index_default_is_all(mixed_kinds_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(mixed_kinds_data, db)
    conn = sqlite3.connect(db)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM messages").fetchall()}
    conn.close()
    assert kinds == {"channel", "dm", "mpim"}


def test_rebuild_is_idempotent(minimal_data: Path, tmp_path: Path):
    db = tmp_path / "search.db"
    index.build_index(minimal_data, db)
    index.build_index(minimal_data, db)  # second call must not double-count
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 3  # the 3 valid messages, not 6

"""ArchiveStore contract — JsonlStore and SqliteStore must behave identically.

The personal profile (JsonlStore, reads data/ jsonl) and the team profile
(SqliteStore, reads the extended search.db) are two backends behind one
interface. If they drift, the same archive renders differently depending on
how it's deployed. This test pins them together:

  * `store` is parametrized over both — every property test runs on each;
  * the `*_stores_agree_*` tests compare the two outputs directly.

Both stores are built from the one `sqlite_with_threads` fixture, so they
describe the exact same archive.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slack_log import indexer, server, splitter
from slack_log.store import JsonlStore, SqliteStore


@pytest.fixture
def archive(sqlite_with_threads: Path, tmp_path: Path):
    """Build the same archive both ways; return (jsonl_store, sqlite_store)."""
    # personal: slackdump.sqlite → splitter → data/ → indexer → personal.db
    data = tmp_path / "data"
    conn = sqlite3.connect(sqlite_with_threads)
    splitter.split(conn, data)
    conn.close()
    personal_db = tmp_path / "personal.db"
    indexer.build_index(data, personal_db, profile="personal")
    jsonl_store = JsonlStore(data_root=data, db_path=personal_db)

    # team: slackdump.sqlite → indexer ETL → team.db
    team_db = tmp_path / "team.db"
    indexer.build_index(sqlite_with_threads, team_db, profile="team")
    sqlite_store = SqliteStore(db_path=team_db)

    return jsonl_store, sqlite_store


@pytest.fixture(params=["jsonl", "sqlite"])
def store(archive, request):
    """Each property test below runs once per store implementation."""
    jsonl_store, sqlite_store = archive
    return jsonl_store if request.param == "jsonl" else sqlite_store


# --- property tests: both implementations must satisfy these ---------------

def test_list_channels(store):
    assert sorted(store.list_channels()) == ["C001", "C002"]


def test_users(store):
    users = store.users()
    assert set(users) == {"U001"}
    assert users["U001"]["real_name"] == "Alice"


def test_thread_meta_standalone(store):
    rows = {r["thread_ts"]: r for r in store.thread_meta("C001")}
    assert set(rows) == {"1700000000.000001", "1700000100.000002"}
    standalone = rows["1700000000.000001"]
    assert standalone["msg_count"] == 1
    assert standalone["is_thread"] is False
    assert standalone["first_text_preview"] == "hello"
    assert standalone["latest_reply_ts"] == "1700000000.000001"
    assert standalone["participants"] == ["U001"]


def test_thread_meta_parent(store):
    parent = {r["thread_ts"]: r for r in store.thread_meta("C001")}["1700000100.000002"]
    assert parent["msg_count"] == 2
    assert parent["is_thread"] is True
    assert parent["reply_count"] == 0
    assert parent["first_user"] == "U001"
    assert parent["first_author_display"] == "Alice"
    assert parent["first_text_preview"] == "parent thread"
    assert parent["latest_reply_ts"] == "1700000101.000003"
    assert parent["has_files"] is False


def test_thread_meta_unknown_channel_is_empty(store):
    assert store.thread_meta("C_NOPE") == []


def test_load_thread(store):
    msgs = store.load_thread("C001", "1700000100.000002")
    assert [m["text"] for m in msgs] == ["parent thread", "reply 1"]


def test_load_thread_standalone(store):
    msgs = store.load_thread("C002", "1700000200.000004")
    assert [m["text"] for m in msgs] == ["good message"]


def test_load_thread_missing_returns_none(store):
    assert store.load_thread("C001", "9999999999.000000") is None
    assert store.load_thread("C_NOPE", "1700000000.000001") is None


def test_global_groups(store):
    g = store.global_groups()
    assert [(c["id"], c["name"], c["thread_count"]) for c in g["channels"]] == [
        ("C001", "general", 2),
        ("C002", "random", 1),
    ]
    assert g["dms"] == []
    assert g["mpims"] == []


def test_global_groups_include_filter(store):
    """include={'dm'} drops both real channels."""
    g = store.global_groups(include={"dm"})
    assert g == {"channels": [], "dms": [], "mpims": []}


def test_search(store):
    hits = store.search("parent")
    assert any("parent" in h["text"] for h in hits)


# --- equivalence tests: the two stores must produce equal output ----------

def test_stores_agree_on_thread_meta(archive):
    jsonl_store, sqlite_store = archive
    for cid in ("C001", "C002"):
        assert jsonl_store.thread_meta(cid) == sqlite_store.thread_meta(cid)


def test_stores_agree_on_load_thread(archive):
    jsonl_store, sqlite_store = archive
    for cid, ts in [("C001", "1700000000.000001"),
                    ("C001", "1700000100.000002"),
                    ("C002", "1700000200.000004")]:
        assert jsonl_store.load_thread(cid, ts) == sqlite_store.load_thread(cid, ts)


def test_stores_agree_on_global_groups(archive):
    jsonl_store, sqlite_store = archive
    assert jsonl_store.global_groups() == sqlite_store.global_groups()


def test_stores_agree_on_users(archive):
    jsonl_store, sqlite_store = archive
    assert jsonl_store.users() == sqlite_store.users()


# --- the team server serves real pages from a SqliteStore -----------------

def test_team_server_serves_pages(archive):
    """create_app on a SqliteStore renders home / channel / thread pages."""
    _, sqlite_store = archive
    client = TestClient(server.create_app(sqlite_store))

    assert client.get("/").status_code == 200

    chan = client.get("/channels/C001")
    assert chan.status_code == 200
    assert "general" in chan.text

    thread = client.get("/channels/C001/threads/1700000100.000002")
    assert thread.status_code == 200
    assert "parent thread" in thread.text
    assert 'id="msg-1700000100.000002"' in thread.text

    assert client.get("/channels/C_NOPE").status_code == 404
    assert client.get("/channels/C001/threads/9999999999.0").status_code == 404

"""slack_log.search CLI + index.search filters (channel/user/date) and CJK.

Guards the fix that makes Chinese search work from the command line: multi-char
Chinese words must match (they go through the same CJK-split FTS transform the
indexer applies), and the channel/user/date filters must narrow correctly.
"""
import json
import sqlite3

import pytest

from slack_log import search as search_cli
from slack_log.core.text import split_cjk
from slack_log.pipeline import index

# text, user_name, channel_name, ts(epoch str), kind
_ROWS = [
    ("视频有黑边需要删除", "Alice", "team", "1781000000.0", "channel"),
    ("分辨率质检通过", "Carol", "team", "1781100000.0", "channel"),
    ("hello world plain ascii", "Bob", "random", "1781200000.0", "channel"),
    ("旧的黑边讨论", "Alice", "team", "1700000000.0", "channel"),  # old
]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "search.db"
    conn = index.open_db(path)
    for text, user, chan, ts, kind in _ROWS:
        index._insert_message(conn, {
            "text": split_cjk(text),  # the indexer normalizes via split_cjk before insert
            "user_name": user, "channel_name": chan, "ts": ts, "thread_ts": None,
            "channel_id": "C1", "user_id": "U1", "kind": kind,
        })
    conn.commit()
    conn.close()
    return path


def _conn(db):
    return sqlite3.connect(db)


def test_fts_query_splits_cjk():
    # multi-char Chinese → per-char phrase so unicode61's char-tokens match
    assert index._to_fts_query("黑边") == '"黑 边"'
    assert index._to_fts_query("质检 分辨率") == '"质 检" "分 辨 率"'


def test_search_multichar_chinese(db):
    hits = index.search(_conn(db), "黑边")
    assert len(hits) == 2
    assert all("黑边" in h["text"] for h in hits)  # display is re-joined


def test_search_channel_filter(db):
    assert len(index.search(_conn(db), "黑边", channel="tea")) == 2
    assert index.search(_conn(db), "黑边", channel="nonexistent") == []


def test_search_user_filter(db):
    hits = index.search(_conn(db), "黑边", user="Alice")
    assert len(hits) == 2 and all(h["user_name"] == "Alice" for h in hits)


def test_search_date_filter(db):
    # after is inclusive; the 1700000000 old message is excluded
    hits = index.search(_conn(db), "黑边", after="1781000000")
    assert len(hits) == 1
    # before is exclusive
    earlier = index.search(_conn(db), "黑边", before="1781000000")
    assert all(float(h["ts"]) < 1781000000 for h in earlier)


def test_cli_json_output(db, capsys):
    rc = search_cli.main(["黑边", "--db", str(db), "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 0 and len(data) == 2


def test_cli_missing_db_errors(tmp_path):
    with pytest.raises(SystemExit):
        search_cli.main(["黑边", "--db", str(tmp_path / "nope.db")])

"""slack_log.unread: review watermark + unread/ack, and the local-midnight date
boundary. Fake data only."""
import datetime as dt
import sqlite3

import pytest

from slack_log import search as search_cli
from slack_log import unread as U
from slack_log.pipeline import index

# 00:30 local (UTC+8) — 16:30 UTC the previous day, inside the hours a
# UTC-midnight boundary silently drops.
TS_0030 = dt.datetime(2026, 1, 15, 0, 30, tzinfo=U.TZ).timestamp()


def _add(path, cid, name, ts, text="please send the forms", kind="channel"):
    conn = index.open_db(path)
    index._insert_message(conn, {
        "text": text, "user_name": "someone", "channel_name": name, "ts": f"{ts:.6f}",
        "thread_ts": None, "channel_id": cid, "user_id": "U1", "kind": kind})
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setattr(U, "TZ", dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(search_cli, "TZ", U.TZ)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "search.db"
    _add(path, "C1", "team", TS_0030)
    return path


def _run(db, **kw):
    conn, rc = sqlite3.connect(db), U.open_review(db)
    try:
        return U.unread(conn, rc, **kw)
    finally:
        conn.close()
        rc.close()


def _ack(db, until):
    conn, rc = sqlite3.connect(db), U.open_review(db)
    try:
        return U.ack(conn, rc, until)
    finally:
        conn.close()
        rc.close()


def test_local_0030_old_boundary_misses_new_hits(db):
    conn = sqlite3.connect(db)
    q = "SELECT COUNT(*) FROM messages WHERE CAST(ts AS REAL) >= {}"
    old = conn.execute(q.format("strftime('%s','2026-01-15')")).fetchone()[0]
    new = conn.execute(q.format(search_cli._date_to_epoch("2026-01-15"))).fetchone()[0]
    assert (old, new) == (0, 1)
    assert _run(db, since=U.date_to_epoch("2026-01-15"))["messages"] == 1
    assert search_cli._fmt_ts(str(TS_0030)) == "2026-01-15 00:30"


def test_new_conversation_listed_in_full(db):
    _ack(db, TS_0030)
    _add(db, "D9", "D9", TS_0030 - 86400, kind="dm")
    _add(db, "D9", "D9", TS_0030 + 60, kind="dm")
    r = _run(db)
    assert [c["chat_id"] for c in r["chats"]] == ["D9"]
    assert r["chats"][0]["new_chat"] and r["chats"][0]["count"] == 2


def test_ack_clears_no_ack_repeats(db):
    a, b = _run(db), _run(db)
    assert a["messages"] == b["messages"] == 1
    _ack(db, a["window"]["until"])
    assert _run(db)["messages"] == 0


def test_ack_until_keeps_later_and_never_regresses(db):
    until = _run(db)["window"]["until"]
    _add(db, "C1", "team", TS_0030 + 600, "arrived after unread")
    _ack(db, until)
    assert _run(db)["messages"] == 1
    _ack(db, TS_0030 - 1)
    assert _run(db)["messages"] == 1


def test_late_arrival_flagged(db):
    _ack(db, TS_0030)
    _add(db, "C1", "team", TS_0030 - 300, "imported late, older ts")
    assert _run(db)["chats"][0]["late"] == 1


def test_cli(db, capsys):
    U.main(["--db", str(db), "--detail"])
    out = capsys.readouterr().out
    assert "please send the forms" in out and "2026-01-15 00:30" in out
    assert f"make ack UNTIL={TS_0030!r}" in out
    U.main(["ack", "--db", str(db), "--until", repr(TS_0030)])
    U.main(["--db", str(db)])
    assert "0 new messages" in capsys.readouterr().out


def test_cli_channel_filter_does_not_offer_ack(db, capsys):
    U.main(["--db", str(db), "--channel", "team"])
    out = capsys.readouterr().out
    assert "please send the forms" in out and "make ack" not in out

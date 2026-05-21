"""Unit tests for slack_log.server — FastAPI app + search API.

The server has three responsibilities:
  1. Serve the existing static html/ tree (so #msg-{ts} anchors still work).
  2. /api/search?q=... — JSON search API.
  3. /search?q=... — HTML page rendering the same hits with click-through links.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slack_log import indexer, server


@pytest.fixture
def app_and_db(tmp_path: Path):
    """Build a tiny search.db + a stub html/ tree, return TestClient bound to them."""
    data = tmp_path / "data"
    c1 = data / "channels" / "C001" / "threads"
    c1.mkdir(parents=True)
    (c1 / "1700000000.000001.jsonl").write_text(
        json.dumps({"ts": "1700000000.000001", "user": "U001",
                    "text": "release notes for v0.7 search"}) + "\n" +
        json.dumps({"ts": "1700000010.000002", "user": "U002",
                    "thread_ts": "1700000000.000001",
                    "text": "今天晚上发布"}) + "\n"
    )
    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "display_name": "alice", "real_name": "Alice",
                 "image_72": "https://x/alice_72.png"},
        "U002": {"name": "bob", "display_name": "bob", "real_name": "Bob"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_channel": True},
    }))

    db = tmp_path / "search.db"
    indexer.build_index(data, db)

    html = tmp_path / "html"
    (html / "channels" / "C001" / "threads").mkdir(parents=True)
    (html / "index.html").write_text("<html>index</html>")

    app = server.create_app(db_path=db, html_root=html, data_root=data)
    return TestClient(app), db, html


def test_healthz(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_search_returns_json_hits(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/search", params={"q": "release"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["query"] == "release"
    assert payload["total"] >= 1
    h = payload["hits"][0]
    # Enough fields to deep-link into the static HTML.
    assert h["channel_id"] == "C001"
    assert h["thread_ts"] == "1700000000.000001"
    assert h["ts"] == "1700000000.000001"
    # Pre-built href the front-end can use directly.
    assert h["url"].endswith("threads/1700000000.000001#msg-1700000000.000001")


def test_api_search_matches_chinese(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/search", params={"q": "发布"})
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_api_search_empty_query(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/search", params={"q": ""})
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_search_html_page(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/search", params={"q": "release"})
    assert r.status_code == 200
    body = r.text
    assert "release" in body
    # Click-through link goes to the static thread page anchor.
    assert "threads/1700000000.000001#msg-1700000000.000001" in body


def test_static_html_served_at_root(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/")
    assert r.status_code == 200
    assert "index" in r.text


def test_channel_page_dynamic_render(app_and_db):
    """/channels/<cid> renders the channel index dynamically from jsonl —
    no pre-built html/ file."""
    client, _, _ = app_and_db
    r = client.get("/channels/C001")
    assert r.status_code == 200
    assert "general" in r.text          # channel name rendered into the page
    assert "<!DOCTYPE html>" in r.text  # a real rendered page, not a static stub
    assert client.get("/channels/C_MISSING").status_code == 404


def test_thread_page_dynamic_render(app_and_db):
    """/channels/<cid>/threads/<ts> renders the thread dynamically from the
    thread jsonl — message text appears in the page."""
    client, _, _ = app_and_db
    r = client.get("/channels/C001/threads/1700000000.000001")
    assert r.status_code == 200
    assert "release" in r.text  # message body rendered
    assert 'id="msg-1700000000.000001"' in r.text  # ref-id anchor
    assert client.get("/channels/C001/threads/9999999999.000000").status_code == 404


def test_api_search_url_has_no_html_suffix(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/search", params={"q": "release"})
    h = r.json()["hits"][0]
    assert h["url"].endswith("threads/1700000000.000001#msg-1700000000.000001")
    assert ".html" not in h["url"]


def test_api_user_returns_messages(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/user/U001")
    assert r.status_code == 200
    d = r.json()
    assert d["user_id"] == "U001"
    assert d["user_name"] == "alice"
    assert d["avatar"] == "https://x/alice_72.png"
    assert d["total"] >= 1
    assert any("release" in m["text"] for m in d["messages"])
    # message rows carry enough metadata to deep-link.
    m = d["messages"][0]
    assert m["channel_id"] == "C001"
    assert m["ts"] == "1700000000.000001"
    assert m["url"].endswith("threads/1700000000.000001#msg-1700000000.000001")


def test_user_html_page(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/user/U001")
    assert r.status_code == 200
    body = r.text
    assert "alice" in body
    assert "alice_72.png" in body
    assert "release" in body
    assert "threads/1700000000.000001#msg-1700000000.000001" in body
    # Human-readable time precedes the message text — not the raw uid/ts.
    assert "2023-" in body  # ts 1700000000 → year 2023
    # Default view = timeline (per latest UX feedback).
    assert "view=by_channel" in body  # link to switch is visible


def test_user_html_by_channel_view(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/user/U001", params={"view": "by_channel"})
    assert r.status_code == 200
    body = r.text
    # Channel grouping appears (channel name shows above its msgs).
    assert "general" in body


def test_api_user_messages_have_human_time(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/user/U001")
    m = r.json()["messages"][0]
    # Human time formatted alongside raw ts for the UI to display.
    assert "human_time" in m
    assert m["human_time"].startswith("20")  # YYYY-...


@pytest.fixture
def app_for_timeline(tmp_path: Path):
    """Same user posts in 3 channels across 3 days — covers all segmenting cases."""
    data = tmp_path / "data"
    for cid in ("C_A", "C_B"):
        (data / "channels" / cid / "threads").mkdir(parents=True)

    # Times chosen so that "newest first" yields this order:
    #   day3 #A msg-a3    ← section A starts (different channel from nothing)
    #   day3 #A msg-a3b   ← same channel + same day  → no channel header, no date
    #   day2 #A msg-a2    ← same channel, different day → no channel header, show date
    #   day2 #B msg-b2    ← different channel → new section, show date
    #   day1 #A msg-a1    ← different channel → new section, show date
    def ts_for(day: int, sec: int) -> str:
        # 2024-01-{day} 12:00:{sec:02d} local time
        from datetime import datetime
        dt = datetime(2024, 1, day, 12, 0, sec)
        return f"{dt.timestamp():.6f}"

    (data / "channels" / "C_A" / "threads" / "1.jsonl").write_text(
        json.dumps({"ts": ts_for(3, 0), "user": "U001", "text": "msg-a3"}) + "\n" +
        json.dumps({"ts": ts_for(3, 1), "user": "U001", "text": "msg-a3b"}) + "\n" +
        json.dumps({"ts": ts_for(2, 0), "user": "U001", "text": "msg-a2"}) + "\n" +
        json.dumps({"ts": ts_for(1, 0), "user": "U001", "text": "msg-a1"}) + "\n"
    )
    (data / "channels" / "C_B" / "threads" / "2.jsonl").write_text(
        json.dumps({"ts": ts_for(2, 5), "user": "U001", "text": "msg-b2"}) + "\n"
    )

    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "display_name": "alice", "image_72": "https://x/a.png"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C_A": {"name": "team-a", "is_channel": True},
        "C_B": {"name": "team-b", "is_channel": True},
    }))
    db = tmp_path / "search.db"
    indexer.build_index(data, db)
    html = tmp_path / "html"
    html.mkdir()
    return TestClient(server.create_app(db_path=db, html_root=html, data_root=data))


def test_api_user_timeline_segments(app_for_timeline):
    """Adjacent same-channel messages collapse into one segment (even across days);
    inside a segment, same-day messages collapse into one day group."""
    r = app_for_timeline.get("/api/user/U001")
    segs = r.json()["segments"]
    # Newest-first ts ordering:
    #   day3 12:00:01 #A a3b · day3 12:00:00 #A a3 · day2 12:00:05 #B b2
    #   · day2 12:00:00 #A a2 · day1 #A a1
    # Adjacent same-channel runs collapse → 3 segments.
    assert [s["channel_name"] for s in segs] == ["team-a", "team-b", "team-a"]
    # First team-a segment: only day3, 2 msgs collapsed in that day.
    s0 = segs[0]
    assert [d["date"] for d in s0["days"]] == ["2024-01-03"]
    assert [m["text"] for m in s0["days"][0]["msgs"]] == ["msg-a3b", "msg-a3"]
    # Last team-a segment spans 2 days (day2 + day1).
    s2 = segs[2]
    assert [d["date"] for d in s2["days"]] == ["2024-01-02", "2024-01-01"]


def test_api_user_by_channel_groups_by_date(app_for_timeline):
    """by_channel view: one segment per distinct channel, days grouped inside."""
    r = app_for_timeline.get("/api/user/U001")
    by_ch = r.json()["by_channel_segments"]
    # Two distinct channels.
    names = sorted(s["channel_name"] for s in by_ch)
    assert names == ["team-a", "team-b"]
    seg_a = next(s for s in by_ch if s["channel_name"] == "team-a")
    # team-a has 4 msgs across 3 dates (newest first).
    assert [d["date"] for d in seg_a["days"]] == ["2024-01-03", "2024-01-02", "2024-01-01"]
    assert sum(len(d["msgs"]) for d in seg_a["days"]) == 4


def test_user_html_by_channel_collapses_dates(app_for_timeline):
    body = app_for_timeline.get("/user/U001", params={"view": "by_channel"}).text
    # Each channel name appears once as a section header.
    assert body.count("#team-a") == 1
    assert body.count("#team-b") == 1
    # Each date label appears once per channel-day pair.
    assert body.count("2024-01-03") == 1   # only in team-a
    assert body.count("2024-01-02") == 2   # team-a once + team-b once
    # Per-message times render client-side as HH:MM:SS from epochs.
    assert 'data-fmt="time"' in body


def test_user_html_timeline_collapses_repeats(app_for_timeline):
    """Channel name appears once per section header; each date label shows once per day."""
    body = app_for_timeline.get("/user/U001").text
    # 2 team-a sections + 1 team-b section.
    assert body.count("#team-a") == 2
    assert body.count("#team-b") == 1
    # Each date appears exactly once per occurrence in segments.
    assert body.count("2024-01-03") == 1   # only in team-a #1
    assert body.count("2024-01-01") == 1   # only in team-a #2
    # Per-message times render client-side as HH:MM:SS only (data-fmt="time"),
    # not the full date again.
    assert 'data-fmt="time"' in body


def test_user_unknown_returns_404(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/user/U_DOES_NOT_EXIST")
    assert r.status_code == 404
    # 404 should be a friendly HTML page, not raw {"detail": ...}.
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert "404" in body
    # Has a search box so the user can recover from a dead link.
    assert "/search" in body


def test_static_not_found_renders_friendly_page(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/channels/C_DOES_NOT_EXIST/index.html")
    assert r.status_code == 404
    assert "text/html" in r.headers.get("content-type", "")
    assert "404" in r.text
    assert "/search" in r.text


def test_api_user_unknown_returns_404(app_and_db):
    client, _, _ = app_and_db
    r = client.get("/api/user/U_DOES_NOT_EXIST")
    assert r.status_code == 404


@pytest.fixture
def app_channel_only(tmp_path: Path):
    """Server bound to a DB that contains both channel + DM messages, but
    create_app is told include={'channel'} so DMs are hidden at runtime."""
    data = tmp_path / "data"
    (data / "channels" / "C001" / "threads").mkdir(parents=True)
    (data / "channels" / "D001" / "threads").mkdir(parents=True)
    (data / "channels" / "C001" / "threads" / "1.jsonl").write_text(
        json.dumps({"ts": "1.0", "user": "U001", "text": "public release"}) + "\n"
    )
    (data / "channels" / "D001" / "threads" / "2.jsonl").write_text(
        json.dumps({"ts": "2.0", "user": "U001", "text": "private chitchat"}) + "\n"
    )
    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "display_name": "alice", "image_72": "https://x/a_72.png"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_channel": True},
        "D001": {"name": "bob-dm", "is_im": True},
    }))
    db = tmp_path / "search.db"
    indexer.build_index(data, db)  # full index — server filters at query time
    html = tmp_path / "html"
    html.mkdir()
    app = server.create_app(db_path=db, html_root=html, data_root=data, include={"channel"})
    return TestClient(app)


def test_channel_only_search_excludes_dm(app_channel_only):
    r = app_channel_only.get("/api/search", params={"q": "release"})
    assert r.json()["total"] == 1
    r2 = app_channel_only.get("/api/search", params={"q": "chitchat"})
    assert r2.json()["total"] == 0


def test_channel_only_user_excludes_dm(app_channel_only):
    r = app_channel_only.get("/api/user/U001")
    msgs = r.json()["messages"]
    assert all(m["channel_id"] == "C001" for m in msgs)
    assert not any("chitchat" in m["text"] for m in msgs)

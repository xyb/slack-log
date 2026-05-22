"""Unit tests for core.slackdump_db — reading slackdump's archive SQLite.

The integration tests exercise the happy path; this file covers the parts they
miss — resolve_author's six-level fallback chain, DM/MPIM name derivation, bot
identity seeding, and chunk dedup.
"""

import json
import sqlite3

from slack_log.core.slackdump_db import (
    _collect_bot,
    _load_channels,
    _load_users,
    _parse,
    _pick_latest,
    resolve_author,
)


# --- _parse ---

def test_parse_str():
    assert _parse('{"a": 1}') == {"a": 1}


def test_parse_bytes():
    assert _parse(b'{"a": 1}') == {"a": 1}


def test_parse_corrupt_json_returns_none():
    assert _parse(b"not json{{{") is None


def test_parse_invalid_utf8_returns_none():
    assert _parse(b"\xff\xfe bad") is None


# --- resolve_author: the six-level fallback chain ---

def test_resolve_author_user_hit():
    users = {"U1": {"display_name": "Alice", "image_48": "a48"}}
    assert resolve_author({"user": "U1"}, users) == ("Alice", "a48")


def test_resolve_author_user_name_priority():
    # display_name → real_name → name → uid
    assert resolve_author({"user": "U1"}, {"U1": {"real_name": "Real"}})[0] == "Real"
    assert resolve_author({"user": "U1"}, {"U1": {"name": "nm"}})[0] == "nm"
    assert resolve_author({"user": "U1"}, {"U1": {}})[0] == "U1"


def test_resolve_author_bot_profile():
    msg = {"bot_profile": {"name": "CI Bot", "icons": {"image_48": "i48"}}}
    assert resolve_author(msg, {}) == ("CI Bot", "i48")


def test_resolve_author_bot_id_in_users():
    users = {"B1": {"real_name": "SeededBot", "image_72": "b72"}}
    assert resolve_author({"bot_id": "B1"}, users) == ("SeededBot", "b72")


def test_resolve_author_username_legacy_field():
    assert resolve_author({"username": "legacy-bot"}, {}) == ("legacy-bot", None)


def test_resolve_author_attachments_fallback():
    msg = {"attachments": [{"service_name": "GitHub", "service_icon": "gh"}]}
    assert resolve_author(msg, {}) == ("GitHub", "gh")


def test_resolve_author_last_resort_bot_id():
    assert resolve_author({"bot_id": "B9"}, {}) == ("bot:B9", None)


def test_resolve_author_last_resort_uid():
    assert resolve_author({"user": "U9"}, {}) == ("U9", None)


def test_resolve_author_unknown():
    assert resolve_author({}, {}) == ("(unknown)", None)


# --- _load_users ---

def test_load_users_extracts_profile_skips_bad_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE S_USER (DATA BLOB)")
    conn.execute("INSERT INTO S_USER (DATA) VALUES (?)", (json.dumps({
        "id": "U1", "name": "alice", "real_name": "Alice",
        "profile": {"display_name": "al", "image_48": "i48"},
    }).encode(),))
    conn.execute("INSERT INTO S_USER (DATA) VALUES (?)", (b"corrupt{{{",))   # skipped
    conn.execute("INSERT INTO S_USER (DATA) VALUES (?)",
                 (json.dumps({"name": "noid"}).encode(),))                   # no id → skipped
    users = _load_users(conn)
    conn.close()
    assert set(users) == {"U1"}
    assert users["U1"]["display_name"] == "al"
    assert users["U1"]["image_48"] == "i48"


# --- _load_channels ---

def _channels_db(rows: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE CHANNEL (DATA BLOB)")
    for r in rows:
        conn.execute("INSERT INTO CHANNEL (DATA) VALUES (?)",
                     (json.dumps(r).encode(),))
    return conn


def test_load_channels_named_channel():
    conn = _channels_db([{"id": "C1", "name": "general", "is_channel": True}])
    ch = _load_channels(conn, {})
    conn.close()
    assert ch["C1"]["name"] == "general"


def test_load_channels_dm_name_derived_from_other_user():
    conn = _channels_db([{"id": "D1", "is_im": True, "user": "U2"}])
    ch = _load_channels(conn, {"U2": {"display_name": "Bob"}})
    conn.close()
    assert ch["D1"]["name"] == "Bob"
    assert ch["D1"]["other_uid"] == "U2"
    assert ch["D1"]["is_im"] is True


def test_load_channels_dm_falls_back_to_uid_when_user_unknown():
    conn = _channels_db([{"id": "D2", "is_im": True, "user": "U9"}])
    ch = _load_channels(conn, {})
    conn.close()
    assert ch["D2"]["name"] == "U9"


def test_load_channels_mpim_name_derived_from_members():
    conn = _channels_db([{"id": "G1", "is_mpim": True, "members": ["U1", "U2"]}])
    ch = _load_channels(conn, {"U1": {"display_name": "A"}, "U2": {"display_name": "B"}})
    conn.close()
    assert ch["G1"]["name"].startswith("mpim:")
    assert "A" in ch["G1"]["name"] and "B" in ch["G1"]["name"]


def test_load_channels_corrupt_row_skipped():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE CHANNEL (DATA BLOB)")
    conn.execute("INSERT INTO CHANNEL (DATA) VALUES (?)", (b"bad{{{",))
    assert _load_channels(conn, {}) == {}
    conn.close()


# --- _collect_bot ---

def test_collect_bot_from_bot_profile():
    users: dict = {}
    _collect_bot({"bot_id": "B1", "bot_profile": {
        "id": "B1", "name": "MyBot", "icons": {"image_48": "i"}}}, users)
    assert users["B1"]["name"] == "MyBot"
    assert users["B1"]["is_bot"] is True


def test_collect_bot_from_bot_add_text():
    users: dict = {}
    _collect_bot({"subtype": "bot_add",
                  "text": "added integration <https://x/services/B7|DeployBot>"}, users)
    assert users["B7"]["name"] == "DeployBot"


def test_collect_bot_does_not_clobber_an_already_named_entry():
    users = {"B1": {"name": "Existing"}}
    _collect_bot({"bot_id": "B1", "bot_profile": {"id": "B1", "name": "New"}}, users)
    assert users["B1"]["name"] == "Existing"


# --- _pick_latest ---

def test_pick_latest_keeps_newest_chunk_per_message():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE MESSAGE (CHANNEL_ID TEXT, TS TEXT, CHUNK_ID INT, LOAD_DTTM TEXT)")
    # the same (C1, 100.1) under two chunks — the newer LOAD_DTTM wins
    conn.execute("INSERT INTO MESSAGE VALUES ('C1', '100.1', 1, '2026-01-01')")
    conn.execute("INSERT INTO MESSAGE VALUES ('C1', '100.1', 2, '2026-01-02')")
    conn.execute("INSERT INTO MESSAGE VALUES ('C1', '100.2', 5, '2026-01-01')")
    keep = _pick_latest(conn)
    conn.close()
    assert keep[("C1", "100.1")] == 2
    assert keep[("C1", "100.2")] == 5

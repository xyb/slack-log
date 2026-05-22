#!/usr/bin/env python3
"""
search.db — the SQLite database the web service reads.

Two profiles feed it:

  personal — build_index(data/, profile="personal"): walks the jsonl layer
    and fills only the `messages` FTS5 table. The personal web server reads
    pages straight from the jsonl files.

  team — build_index(slackdump.sqlite, profile="team"): an ETL straight off
    slackdump's archive, no jsonl in between. Fills `messages` plus the
    materialized `message_raw` / `threads` / `channels` / `users` tables, so
    the team web server can serve every page from search.db alone.

search.db keeps slack-log's own stable schema — decoupled from slackdump, so
a slackdump schema change can't break the web layer.
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from tqdm import tqdm

from slack_log.core.slackdump_db import (
    _collect_bot,
    _load_channels,
    _load_users,
    _parse,
    _pick_latest,
    resolve_author,
)
from slack_log.core.text import (
    CJK_CHAR,
    channel_name,
    join_cjk,
    make_preview,
    normalize_text,
)

VALID_KINDS = {"channel", "dm", "mpim"}


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open / create search.db. Idempotent — every table is IF NOT EXISTS, so
    an old messages-only search.db gains the team tables on the next open."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            text,
            user_name,
            channel_name,
            ts UNINDEXED,
            thread_ts UNINDEXED,
            channel_id UNINDEXED,
            user_id UNINDEXED,
            kind UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );

        -- Team profile: the complete message JSON, for rendering a thread page.
        CREATE TABLE IF NOT EXISTS message_raw (
            channel_id TEXT NOT NULL,
            ts         TEXT NOT NULL,
            thread_ts  TEXT,
            data       TEXT NOT NULL,
            PRIMARY KEY (channel_id, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_message_raw_thread
            ON message_raw (channel_id, thread_ts, ts);

        -- Team profile: materialized per-thread metadata. Columns mirror the
        -- splitter's index.jsonl entry one-for-one.
        CREATE TABLE IF NOT EXISTS threads (
            channel_id           TEXT NOT NULL,
            thread_ts            TEXT NOT NULL,
            first_ts             TEXT,
            first_user           TEXT,
            first_author_display TEXT,
            first_author_avatar  TEXT,
            first_text_preview   TEXT,
            latest_reply_ts      TEXT,
            reply_count          INTEGER,
            msg_count            INTEGER,
            is_thread            INTEGER,
            has_files            INTEGER,
            participants         TEXT,
            PRIMARY KEY (channel_id, thread_ts)
        );

        -- Team profile: channel + user directories.
        CREATE TABLE IF NOT EXISTS channels (
            id           TEXT PRIMARY KEY,
            name         TEXT,
            kind         TEXT,
            is_im        INTEGER,
            is_mpim      INTEGER,
            other_uid    TEXT,
            members      TEXT,
            thread_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS users (
            id      TEXT PRIMARY KEY,
            profile TEXT
        );
        """
    )
    return conn


def _kind_of(cid: str, channels: dict) -> str:
    c = channels.get(cid) or {}
    if c.get("is_im"):
        return "dm"
    if c.get("is_mpim"):
        return "mpim"
    return "channel"


# --- personal source: the jsonl data layer --------------------------------


def iter_messages(data_root: Path) -> Iterator[dict]:
    """Yield {ts, thread_ts, channel_id, channel_name, kind, user_id, user_name, text}
    from the jsonl data layer."""
    users = json.loads((data_root / "users.json").read_text()) if (data_root / "users.json").exists() else {}
    channels = json.loads((data_root / "channels.json").read_text()) if (data_root / "channels.json").exists() else {}

    channels_root = data_root / "channels"
    if not channels_root.exists():
        return
    for cdir in sorted(channels_root.iterdir()):
        if not cdir.is_dir():
            continue
        threads_dir = cdir / "threads"
        if not threads_dir.exists():
            continue
        cname = channel_name(cdir.name, channels)
        ckind = _kind_of(cdir.name, channels)
        for jsonl in sorted(threads_dir.glob("*.jsonl")):
            try:
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = msg.get("ts")
                if not ts:
                    continue
                uid = msg.get("user") or msg.get("bot_id") or ""
                author_name, _ = resolve_author(msg, users)
                yield {
                    "ts": ts,
                    "thread_ts": msg.get("thread_ts") or ts,
                    "channel_id": cdir.name,
                    "channel_name": cname,
                    "kind": ckind,
                    "user_id": uid,
                    "user_name": author_name if author_name != "(unknown)" else "",
                    "text": normalize_text(msg.get("text") or "", users, channels),
                }


# --- team source: slackdump's archive SQLite ------------------------------


def iter_messages_from_sqlite(sqlite_path: Path) -> Iterator[dict]:
    """Yield FTS records straight from slackdump.sqlite — the team profile's
    message source, with no jsonl in between. Same record shape as
    iter_messages().

    Two passes: the first seeds bot identities from every kept message, so
    resolve_author() in the second pass sees a complete users map (exactly as
    the splitter does); the second yields normalized records. Chunk dedup is
    core.slackdump_db._pick_latest.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        users = _load_users(conn)
        channels = _load_channels(conn, users)
        keep = _pick_latest(conn)
        for cid, ts, chunk_id, data in conn.execute(
            "SELECT CHANNEL_ID, TS, CHUNK_ID, DATA FROM MESSAGE"
        ):
            if keep.get((cid, ts)) != chunk_id:
                continue
            msg = _parse(data)
            if msg is not None:
                _collect_bot(msg, users)
        for cid, ts, chunk_id, data in conn.execute(
            "SELECT CHANNEL_ID, TS, CHUNK_ID, DATA FROM MESSAGE"
        ):
            if keep.get((cid, ts)) != chunk_id:
                continue
            msg = _parse(data)
            if msg is None:
                continue
            uid = msg.get("user") or msg.get("bot_id") or ""
            author_name, _ = resolve_author(msg, users)
            yield {
                "ts": ts,
                "thread_ts": msg.get("thread_ts") or ts,
                "channel_id": cid,
                "channel_name": channel_name(cid, channels),
                "kind": _kind_of(cid, channels),
                "user_id": uid,
                "user_name": author_name if author_name != "(unknown)" else "",
                "text": normalize_text(msg.get("text") or "", users, channels),
            }
    finally:
        conn.close()


# --- build ----------------------------------------------------------------


def _insert_message(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO messages (text, user_name, channel_name, ts, thread_ts, channel_id, user_id, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (r["text"], r["user_name"], r["channel_name"], r["ts"], r["thread_ts"],
         r["channel_id"], r.get("user_id", ""), r["kind"]),
    )


def _index_personal(data_root: Path, conn: sqlite3.Connection, include: set[str]) -> dict:
    """Personal profile — fill the messages FTS table from the jsonl layer."""
    rows = [r for r in iter_messages(data_root) if r["kind"] in include]
    with conn:
        for r in tqdm(rows, desc="indexing", unit="msg", disable=not rows):
            _insert_message(conn, r)
    return {"indexed": len(rows)}


def _index_team(sqlite_path: Path, conn: sqlite3.Connection, include: set[str]) -> dict:
    """Team profile — ETL slackdump.sqlite into the full search.db schema:
    messages FTS + message_raw + threads + channels + users."""
    src = sqlite3.connect(sqlite_path)
    try:
        users = _load_users(src)
        channels = _load_channels(src, users)
        keep = _pick_latest(src)

        # One scan of the message blobs: seed bots, write message_raw, and
        # accumulate per-thread metadata (mirrors splitter._stream_split).
        index_meta: dict = {}
        with conn:
            for cid, ts, chunk_id, anchor, is_parent, latest_reply, data in src.execute(
                "SELECT CHANNEL_ID, TS, CHUNK_ID, COALESCE(THREAD_TS, TS), "
                "IS_PARENT, LATEST_REPLY, DATA FROM MESSAGE"
            ):
                if keep.get((cid, ts)) != chunk_id:
                    continue
                if include and _kind_of(cid, channels) not in include:
                    continue
                msg = _parse(data)
                if msg is None:
                    continue
                _collect_bot(msg, users)
                conn.execute(
                    "INSERT OR REPLACE INTO message_raw (channel_id, ts, thread_ts, data) "
                    "VALUES (?, ?, ?, ?)",
                    (cid, ts, msg.get("thread_ts") or ts,
                     json.dumps(msg, ensure_ascii=False)),
                )
                meta = index_meta.setdefault((cid, anchor), {"msg_count": 0})
                if ts == anchor:
                    meta["is_parent"] = is_parent
                    meta["latest_reply"] = latest_reply
                meta["msg_count"] += 1
                if ts == anchor or "first" not in meta:
                    meta["first"] = msg
                    meta["first_ts"] = ts

        # threads table — one row per anchor with a surviving message.
        thread_count: dict = {}
        with conn:
            for (cid, anchor), meta in index_meta.items():
                if "first" not in meta:
                    continue
                thread_count[cid] = thread_count.get(cid, 0) + 1
                first = meta["first"]
                display, avatar = resolve_author(first, users)
                lr = meta.get("latest_reply")
                conn.execute(
                    "INSERT OR REPLACE INTO threads (channel_id, thread_ts, first_ts, "
                    "first_user, first_author_display, first_author_avatar, "
                    "first_text_preview, latest_reply_ts, reply_count, msg_count, "
                    "is_thread, has_files, participants) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid, anchor, meta["first_ts"], first.get("user"),
                        display, avatar,
                        make_preview(first.get("text") or "", users, channels),
                        lr if lr and lr != "0000000000.000000" else meta["first_ts"],
                        first.get("reply_count", 0), meta["msg_count"],
                        1 if meta.get("is_parent") else 0,
                        1 if first.get("files") else 0,
                        json.dumps(
                            list({first.get("user")} | set(first.get("reply_users") or []))
                            if first.get("user") else []
                        ),
                    ),
                )

        # users + channels directories.
        with conn:
            for uid, prof in users.items():
                conn.execute(
                    "INSERT OR REPLACE INTO users (id, profile) VALUES (?, ?)",
                    (uid, json.dumps(prof, ensure_ascii=False)),
                )
            for cid, meta in channels.items():
                if include and _kind_of(cid, channels) not in include:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO channels (id, name, kind, is_im, is_mpim, "
                    "other_uid, members, thread_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid, meta["name"], _kind_of(cid, channels),
                        1 if meta.get("is_im") else 0,
                        1 if meta.get("is_mpim") else 0,
                        meta.get("other_uid"),
                        json.dumps(meta.get("members") or []),
                        thread_count.get(cid, 0),
                    ),
                )

        # messages FTS — normalized text from slackdump.sqlite.
        indexed = 0
        with conn:
            for r in iter_messages_from_sqlite(sqlite_path):
                if include and r["kind"] not in include:
                    continue
                _insert_message(conn, r)
                indexed += 1
    finally:
        src.close()
    return {"indexed": indexed}


def build_index(source: Path, db_path: Path, include: set[str] | None = None,
                profile: str = "personal") -> dict:
    """Full rebuild of search.db.

    source  — the jsonl data/ directory (personal) or slackdump.sqlite (team).
    profile — "personal" or "team"; selects the source reader and whether the
              team tables get filled.
    include — subset of {channel, dm, mpim}. None = all.
    """
    if include is None:
        include = set(VALID_KINDS)
    bad = include - VALID_KINDS
    if bad:
        raise ValueError(f"include only accepts {VALID_KINDS}, got {bad}")
    if profile not in ("personal", "team"):
        raise ValueError(f"profile must be 'personal' or 'team', got {profile!r}")

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = open_db(db_path)
    try:
        if profile == "team":
            return _index_team(Path(source), conn, include)
        return _index_personal(Path(source), conn, include)
    finally:
        conn.close()


# --- search ---------------------------------------------------------------


def _to_fts_query(q: str) -> str:
    """Quote every whitespace-separated word as a phrase, AND them together.

    FTS5 treats `AND` / `OR` / `NOT` / `NEAR` (uppercase), `column:term`,
    leading `@` / `^`, unterminated `"`, etc. as query syntax. Letting raw
    user input through is a 500-error generator.

    Wrapping each word as `"..."` makes FTS5 treat the contents as literal text.
    unicode61 still tokenizes inside the phrase, so `"@alice"` becomes the
    token `alice` and matches; `"AND"` becomes the literal token `and`.

    CJK runs are split char-by-char inside the phrase to mirror the index-time
    split_cjk transform: `<CJK1><CJK2>` -> `"<CJK1> <CJK2>"` -> AND of two tokens.
    """
    parts: list[str] = []
    for word in q.split():
        word = CJK_CHAR.sub(lambda m: " " + m.group(0) + " ", word)
        word = re.sub(r"\s+", " ", word).strip()
        if not word:
            continue
        word = word.replace('"', '""')
        parts.append(f'"{word}"')
    return " ".join(parts)


def search(conn: sqlite3.Connection, query: str, limit: int = 50,
           include: set[str] | None = None) -> list[dict]:
    """FTS5 MATCH query returning hits with HTML-marked snippet.

    include: subset of {channel, dm, mpim} to limit results by kind.
    """
    if not query.strip():
        return []
    query = _to_fts_query(query)
    if not query:
        return []
    kind_clause, kind_params = "", []
    if include:
        ph = ",".join("?" * len(include))
        kind_clause = f" AND kind IN ({ph})"
        kind_params = sorted(include)
    cur = conn.execute(
        f"""
        SELECT ts, thread_ts, channel_id, channel_name, user_name, text,
               snippet(messages, 0, '<mark>', '</mark>', '…', 16) AS snippet
        FROM messages
        WHERE messages MATCH ?{kind_clause}
        ORDER BY rank
        LIMIT ?
        """,
        (query, *kind_params, limit),
    )
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        d["text"] = join_cjk(d.get("text") or "")
        d["snippet"] = join_cjk(d.get("snippet") or "")
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description="Build search.db from a jsonl layer or slackdump.sqlite")
    ap.add_argument("--data", type=Path, default=Path("./data"),
                    help="personal profile: the jsonl data/ directory")
    ap.add_argument("--sqlite", type=Path, default=None,
                    help="team profile: path to slackdump.sqlite (overrides --data)")
    ap.add_argument("--db", type=Path, default=Path("./search.db"))
    ap.add_argument("--profile", choices=("personal", "team"), default="personal")
    ap.add_argument("--include", default="",
                    help="comma-separated subset of channel/dm/mpim (default: all)")
    args = ap.parse_args()

    include = {p.strip() for p in args.include.split(",") if p.strip()} or None
    if args.profile == "team":
        source = args.sqlite or Path("raw/slackdump.sqlite")
    else:
        source = args.data
    stats = build_index(source, args.db, include=include, profile=args.profile)
    print(f"✅ [{args.profile}] indexed {stats['indexed']} messages → {args.db}")


if __name__ == "__main__":
    main()

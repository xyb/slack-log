#!/usr/bin/env python3
"""
data/ → search.db (SQLite FTS5)

Walk every thread JSONL, normalize each message (resolve mention uids to
display names, expand :shortcode: emoji, strip Slack <...> link wrappers),
and write it into a standalone FTS5 database for v0.7 web search.

Standalone — decoupled from slackdump.sqlite so slackdump schema changes
don't break search.
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterator

import emoji
from tqdm import tqdm

CJK_CHAR = re.compile(r"[㐀-鿿豈-﫿]")
CJK_RUN_GE2 = re.compile(r"[㐀-鿿豈-﫿]{2,}")


JOIN_CJK = re.compile(r"(?<=[㐀-鿿豈-﫿])\s+(?=[㐀-鿿豈-﫿])")


def join_cjk(text: str) -> str:
    """Inverse of split_cjk for display: collapse the space between two CJK chars."""
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = JOIN_CJK.sub("", text)
    return text


def split_cjk(text: str) -> str:
    """Insert spaces between CJK ideographs so unicode61 tokenizes them one-by-one.

    Why: unicode61 has no separator between CJK chars, so an entire Chinese run
    becomes one token. Searching "发布" inside "今天发布新版本" then misses.
    Pre-splitting at index time makes "发布" a 2-token phrase that matches.
    """
    return CJK_CHAR.sub(lambda m: " " + m.group(0) + " ", text)


USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
CHANNEL_MENTION = re.compile(r"<#([C][A-Z0-9]+)(?:\|([^>]+))?>")
LINK_WITH_LABEL = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
LINK_BARE = re.compile(r"<(https?://[^>\s]+)>")
SPECIAL_MENTION = re.compile(r"<!(here|channel|everyone)(?:\|[^>]+)?>")


def display_name(uid: str | None, users: dict) -> str:
    if not uid:
        return ""
    u = users.get(uid) or {}
    return u.get("display_name") or u.get("real_name") or u.get("name") or uid


def channel_name(cid: str, channels: dict) -> str:
    return (channels.get(cid) or {}).get("name") or cid


def normalize_text(text: str, users: dict, channels: dict) -> str:
    """Resolve mention uids / channel cids, strip Slack <link> wrappers, emojize."""
    if not text:
        return ""

    def repl_user(m: re.Match) -> str:
        label = m.group(2)
        if label:
            return f"@{label}"
        return f"@{display_name(m.group(1), users)}"

    def repl_channel(m: re.Match) -> str:
        label = m.group(2)
        if label:
            return f"#{label}"
        return f"#{channel_name(m.group(1), channels)}"

    text = USER_MENTION.sub(repl_user, text)
    text = CHANNEL_MENTION.sub(repl_channel, text)
    text = SPECIAL_MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = LINK_WITH_LABEL.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = LINK_BARE.sub(lambda m: m.group(1), text)
    text = emoji.emojize(text, language="alias")
    return split_cjk(text)


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open / create the search database. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
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
        )
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


def iter_messages(data_root: Path) -> Iterator[dict]:
    """Yield {ts, thread_ts, channel_id, channel_name, kind, user_id, user_name, text}."""
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
                from slack_log.splitter import resolve_author
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


VALID_KINDS = {"channel", "dm", "mpim"}


def build_index(data_root: Path, db_path: Path, include: set[str] | None = None) -> dict:
    """Full rebuild — drops + recreates the messages table, then populates.

    include: subset of {channel, dm, mpim}. None = all.
    """
    if include is None:
        include = set(VALID_KINDS)
    bad = include - VALID_KINDS
    if bad:
        raise ValueError(f"include only accepts {VALID_KINDS}, got {bad}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = open_db(db_path)
    indexed = 0
    rows = [r for r in iter_messages(data_root) if r["kind"] in include]
    with conn:
        for r in tqdm(rows, desc="indexing", unit="msg", disable=not rows):
            conn.execute(
                "INSERT INTO messages (text, user_name, channel_name, ts, thread_ts, channel_id, user_id, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r["text"], r["user_name"], r["channel_name"], r["ts"], r["thread_ts"],
                 r["channel_id"], r.get("user_id", ""), r["kind"]),
            )
            indexed += 1
    conn.close()
    return {"indexed": indexed}


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
    ap = argparse.ArgumentParser(description="Build FTS5 search index from JSONL data layer")
    ap.add_argument("--data", type=Path, default=Path("./data"))
    ap.add_argument("--db", type=Path, default=Path("./search.db"))
    ap.add_argument("--include", default="",
                    help="comma-separated subset of channel/dm/mpim (default: all)")
    args = ap.parse_args()

    include = {p.strip() for p in args.include.split(",") if p.strip()} or None
    stats = build_index(args.data, args.db, include=include)
    print(f"✅ indexed {stats['indexed']} messages → {args.db}")


if __name__ == "__main__":
    main()

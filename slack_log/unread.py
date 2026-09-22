"""Unread: list every conversation's new messages since a per-conversation
review watermark, and advance that watermark explicitly once you're done.

    python3 -m slack_log.unread                  # summary: conversation x count
    python3 -m slack_log.unread --detail         # expand the messages
    python3 -m slack_log.unread --since 2026-09-01   # ignore watermarks (re-scan)
    python3 -m slack_log.unread ack --until 1790060065.999709

Why a watermark: a daily scan built from an ad-hoc query with a hand-picked
date boundary and a hand-picked channel list misses things silently. Here every
conversation in search.db is covered (no allow-list), a conversation seen for
the first time has no watermark and is listed in full, and `unread` never moves
the watermark — only `ack` does, up to the `until` that `unread` printed.

Time zone contract, fixed here so callers don't have to remember it:
  search.db `ts` is a Slack ts — a real UTC epoch (seconds.micro, as TEXT).
  Human dates (--since, printed times) are in TZ: $SLACK_LOG_TZ, default
  Asia/Shanghai. Never build a boundary with sqlite strftime('%s','YYYY-MM-DD'):
  that is UTC midnight, which silently drops the first hours of a local day.

The watermark lives in its own review.db next to search.db, because a rebuild
deletes and recreates search.db.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from slack_log.core.text import join_cjk

TZ = ZoneInfo(os.environ.get("SLACK_LOG_TZ") or "Asia/Shanghai")
SOURCE = "slack"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "search.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_watermark (
    source               TEXT NOT NULL,   -- wechat | slack
    chat_id              TEXT NOT NULL,   -- Slack channel id
    last_reviewed_ts     REAL NOT NULL,   -- reviewed up to this ts (inclusive)
    last_reviewed_msg_id TEXT,            -- that message's ts string
    reviewed_count       INTEGER,         -- messages with ts <= watermark at ack time
    reviewed_at          TEXT NOT NULL,
    PRIMARY KEY (source, chat_id)
);
"""


def date_to_epoch(s: str) -> float:
    """YYYY-MM-DD -> epoch of local (TZ) midnight."""
    return _dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ).timestamp()


def fmt_ts(ts) -> str:
    return _dt.datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d %H:%M")


def open_review(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).with_name("review.db"))
    conn.executescript(SCHEMA)
    return conn


def _rows(conn):
    """(channel_id, channel_name, kind, ts_float, ts_str) for every message."""
    return conn.execute(
        "SELECT channel_id, channel_name, kind, CAST(ts AS REAL), ts FROM messages").fetchall()


def unread(conn, rconn, *, since=None, until=None, detail=False, channel=None) -> dict:
    """New messages after each conversation's watermark, up to `until` (inclusive,
    default the newest ts in search.db). `since` ignores watermarks."""
    rows = _rows(conn)
    newest = max((r[3] for r in rows), default=0.0)
    until = float(until) if until is not None else newest
    wms = {r[0]: (r[1], r[2]) for r in rconn.execute(
        "SELECT chat_id, last_reviewed_ts, reviewed_count FROM review_watermark WHERE source=?",
        (SOURCE,))}
    chats: dict[str, dict] = {}
    below: dict[str, int] = {}
    for cid, name, kind, ts, _ in rows:
        c = chats.setdefault(cid, {"chat_id": cid, "name": name, "kind": kind,
                                   "new_chat": since is None and cid not in wms,
                                   "count": 0, "first_ts": None, "last_ts": None, "late": 0})
        wm = wms.get(cid, (None,))[0]
        lo = since if since is not None else (wm if wm is not None else float("-inf"))
        if wm is not None and ts <= wm:
            below[cid] = below.get(cid, 0) + 1
        if lo < ts <= until:
            c["count"] += 1
            c["first_ts"] = ts if c["first_ts"] is None else min(c["first_ts"], ts)
            c["last_ts"] = ts if c["last_ts"] is None else max(c["last_ts"], ts)
    if since is None:
        # ponytail: a count check only says *that* something landed below the
        # watermark after ack (late import), not which message; storing every
        # reviewed ts would pin it down
        for cid, (_, cnt) in wms.items():
            if cid in chats and cnt is not None:
                chats[cid]["late"] = max(below.get(cid, 0) - cnt, 0)
    listed = [c for c in chats.values() if (c["count"] or c["late"])
              and (not channel or channel in c["chat_id"] or channel in (c["name"] or ""))]
    if detail:
        for c in listed:
            wm = wms.get(c["chat_id"], (None,))[0]
            lo = since if since is not None else (wm if wm is not None else float("-inf"))
            c["messages"] = [
                {"ts": ts, "user_name": u, "text": join_cjk(t or ""), "thread_ts": th}
                for ts, u, t, th in conn.execute(
                    "SELECT ts, user_name, text, thread_ts FROM messages WHERE channel_id=? "
                    "AND CAST(ts AS REAL)>? AND CAST(ts AS REAL)<=? ORDER BY CAST(ts AS REAL)",
                    (c["chat_id"], lo, until))]
    listed.sort(key=lambda c: (not c["late"], not c["new_chat"], -c["count"]))
    lows = [since] if since is not None else [w[0] for w in wms.values()]
    return {
        "source": SOURCE,
        "window": {"from": min(lows) if lows else None, "until": until,
                   "mode": "since" if since is not None else "watermark"},
        "newest_ts": newest,
        "lag_minutes": int((time.time() - newest) / 60) if newest else None,
        "chats_total": len(chats),
        "chats_with_watermark": len(wms),
        "chats_new": sum(1 for c in listed if c["new_chat"]),
        "messages": sum(c["count"] for c in listed),
        "chats": listed,
    }


def ack(conn, rconn, until) -> int:
    """Advance every conversation's watermark to its last message at or before
    `until`. Never moves a watermark backwards. Returns conversations touched."""
    until = float(until)
    last: dict[str, tuple[float, str, int]] = {}
    for cid, _, _, ts, ts_str in _rows(conn):
        if ts <= until:
            prev = last.get(cid, (float("-inf"), None, 0))
            last[cid] = (max(prev[0], ts), ts_str if ts >= prev[0] else prev[1], prev[2] + 1)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rconn.executemany(
        """INSERT INTO review_watermark
               (source, chat_id, last_reviewed_ts, last_reviewed_msg_id, reviewed_count, reviewed_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, chat_id) DO UPDATE SET
               last_reviewed_ts=excluded.last_reviewed_ts,
               last_reviewed_msg_id=excluded.last_reviewed_msg_id,
               reviewed_count=excluded.reviewed_count,
               reviewed_at=excluded.reviewed_at
           WHERE excluded.last_reviewed_ts >= review_watermark.last_reviewed_ts""",
        [(SOURCE, cid, ts, s, n, now) for cid, (ts, s, n) in last.items()])
    rconn.commit()
    return len(last)


def _print(r: dict, max_per_chat: int, partial: bool = False) -> None:
    w = r["window"]
    frm = fmt_ts(w["from"]) if w["from"] is not None else "(no watermark yet: everything)"
    print(f"window: {frm} ~ {fmt_ts(w['until'])}  (epoch {w['from']} ~ {w['until']}, "
          f"{'--since' if w['mode'] == 'since' else 'per-conversation watermark, earliest shown'})")
    lag = r["lag_minutes"]
    warn = "  ⚠ over 3h stale: run the build first, a zero may be a false negative" \
        if lag is not None and lag > 180 else ""
    print(f"pipeline: newest message {fmt_ts(r['newest_ts'])}, {lag} min ago{warn}")
    print(f"conversations: {r['chats_total']} in archive, {r['chats_with_watermark']} with a "
          f"watermark; {len(r['chats'])} with new messages ({r['chats_new']} seen for the first "
          f"time); {r['messages']} new messages")
    print()
    for c in r["chats"]:
        tags = ("🆕" if c["new_chat"] else "") + (f" ⚠ {c['late']} late" if c["late"] else "")
        span = f"{fmt_ts(c['first_ts'])} ~ {fmt_ts(c['last_ts'])}" if c["count"] else ""
        print(f"{c['count']:>5}  #{c['name']}  [{c['kind']}] {c['chat_id']} {tags} {span}")
        msgs = c.get("messages", [])
        for m in msgs[-max_per_chat:] if max_per_chat else msgs:
            reply = " ↳" if m["thread_ts"] and m["thread_ts"] != m["ts"] else ""
            print(f"      {fmt_ts(m['ts'])}{reply} {m['user_name']}: {m['text'].replace(chr(10), ' ')}")
        if max_per_chat and len(msgs) > max_per_chat:
            print(f"      … {len(msgs) - max_per_chat} earlier, use --channel X --max-per-chat 0")
    if partial:
        # ack advances every conversation; acking after a filtered view would
        # swallow the new messages of conversations that were never shown
        print("\n(--channel showed only some conversations: do not ack from this view)")
    elif r["chats"] and w["mode"] == "watermark":
        print(f"\nafter reviewing, advance the watermark: make ack UNTIL={w['until']!r}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ack"]:
        ap = argparse.ArgumentParser(prog="python3 -m slack_log.unread ack")
        ap.add_argument("--db", default=str(DEFAULT_DB))
        ap.add_argument("--until", type=float, required=True,
                        help="the epoch `unread` printed — not the current time")
        a = ap.parse_args(argv[1:])
    else:
        ap = argparse.ArgumentParser(prog="python3 -m slack_log.unread",
                                     description="New messages since the review watermark, "
                                                 "across every archived conversation.")
        ap.add_argument("--db", default=str(DEFAULT_DB))
        ap.add_argument("--since", help="ignore watermarks; from local midnight of YYYY-MM-DD")
        ap.add_argument("--until", type=float, help="window end epoch (default: newest message)")
        ap.add_argument("--channel", help="only conversations whose id/name contains this; implies --detail")
        ap.add_argument("--detail", action="store_true", help="expand messages")
        ap.add_argument("--max-per-chat", type=int, default=30,
                        help="with --detail, show the last N per conversation (0 = all)")
        ap.add_argument("--json", action="store_true", help="JSON, messages included")
        a = ap.parse_args(argv)
    if not Path(a.db).exists():
        ap.error(f"search.db not found at {a.db} — run `make personal-build` first")
    conn, rconn = sqlite3.connect(a.db), open_review(a.db)
    try:
        if argv[:1] == ["ack"]:
            n = ack(conn, rconn, a.until)
            print(f"watermark advanced to {fmt_ts(a.until)} (epoch {a.until!r}), {n} conversations")
            return 0
        try:
            since = date_to_epoch(a.since) if a.since else None
        except ValueError:
            ap.error("--since must be YYYY-MM-DD")
        r = unread(conn, rconn, since=since, until=a.until,
                   detail=a.detail or a.json or bool(a.channel), channel=a.channel)
    finally:
        conn.close()
        rconn.close()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        _print(r, a.max_per_chat, partial=bool(a.channel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

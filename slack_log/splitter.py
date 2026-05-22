#!/usr/bin/env python3
"""slackdump.sqlite → data/ : per-thread jsonl + per-channel index.jsonl.

Reuses slackdump's archived SQLite — no re-collection.

Three passes, none with ORDER BY (no sqlite sort temp file):

  1. Light pass — CHANNEL_ID/TS/CHUNK_ID/LOAD_DTTM only (never the DATA
     blob). slackdump stores one message under several chunks; pick the
     newest-LOAD_DTTM chunk per (channel, ts) for dedup. (core.slackdump_db)
  2. Streaming pass — append every kept message straight into its thread's
     jsonl through an LRU handle cache. No thread is buffered in memory.
  3. Tidy pass — re-read each jsonl just written; slackdump's table order
     is not chronological, so the few files that came out unordered are
     sorted by ts and rewritten. A file already ordered is left untouched.
     For an incremental run this touches only the handful of changed files.

Memory is O(per-thread index metadata + a bounded handle cache); IO is two
table scans + appends + a re-read of the just-written (cache-hot) jsonl.
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path

from slack_log.core.slackdump_db import (
    _collect_bot,
    _load_channels,
    _load_users,
    _parse,
    _pick_latest,
    resolve_author,
)
from slack_log.core.text import make_preview


class _HandleCache:
    """LRU cache of open jsonl handles, so streaming appends rarely reopen.

    First open of a path truncates (a split run rewrites every thread it
    touches); a path reopened after eviction appends, keeping earlier lines.
    `limit` bounds the number of simultaneously open file descriptors.
    """

    def __init__(self, limit: int = 256):
        self._limit = limit
        self._open: "OrderedDict[Path, object]" = OrderedDict()
        self._seen: set = set()

    def write(self, path: Path, line: str) -> None:
        fh = self._open.get(path)
        if fh is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a" if path in self._seen else "w")
            self._seen.add(path)
            self._open[path] = fh
            if len(self._open) > self._limit:
                _, evicted = self._open.popitem(last=False)
                evicted.close()
        else:
            self._open.move_to_end(path)
        fh.write(line)

    def close_all(self) -> None:
        for fh in self._open.values():
            fh.close()
        self._open.clear()

    def seen(self) -> set:
        """Every jsonl path written this run — the tidy pass's work list."""
        return self._seen


def _stream_split(conn: sqlite3.Connection, out_root: Path, users: dict,
                  keep_chunk: dict) -> dict:
    """Pass 2 — stream each kept message straight into its thread's jsonl
    via the LRU handle cache. No thread is buffered. Returns index metadata
    keyed by (cid, anchor)."""
    index_meta: dict = {}
    fc = _HandleCache()
    cur = conn.execute(
        "SELECT CHANNEL_ID, TS, CHUNK_ID, COALESCE(THREAD_TS, TS) AS anchor, "
        "IS_PARENT, LATEST_REPLY, DATA FROM MESSAGE"
    )
    try:
        for cid, ts, chunk_id, anchor, is_parent, latest_reply, data in cur:
            if keep_chunk.get((cid, ts)) != chunk_id:
                continue  # a superseded duplicate chunk
            meta = index_meta.setdefault((cid, anchor), {"msg_count": 0})
            if ts == anchor:
                # The anchor (parent / standalone) row carries thread-level
                # flags — read from columns, so they survive a corrupt blob.
                meta["is_parent"] = is_parent
                meta["latest_reply"] = latest_reply
            msg = _parse(data)
            if msg is None:
                print(f"⚠️  {cid}/{anchor} ts={ts}: skip corrupt row", file=sys.stderr)
                continue
            fc.write(
                out_root / "channels" / cid / "threads" / f"{anchor}.jsonl",
                json.dumps(msg, ensure_ascii=False) + "\n",
            )
            _collect_bot(msg, users)
            meta["msg_count"] += 1
            if ts == anchor or "first" not in meta:
                # anchor msg always wins; otherwise the first kept msg seen
                # is a provisional first (until/unless the anchor shows up).
                meta["first"] = msg
                meta["first_ts"] = ts
    finally:
        fc.close_all()
    return index_meta, fc.seen()


def _sort_unordered(written_paths: set) -> int:
    """Pass 3 — re-sort the thread jsonl files that came out unordered.

    slackdump's table order is not chronological, so a streamed append can
    leave a thread's lines out of ts order. Re-read each file just written
    (cache-hot); one already in ts order is left untouched — no rewrite, no
    IO. An incremental run only appends to a few threads, so only those few
    files are re-read here. Returns the number of files rewritten.
    """
    fixed = 0
    for path in written_paths:
        try:
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        except OSError:
            continue
        msgs = []
        for ln in lines:
            try:
                msgs.append(json.loads(ln))
            except json.JSONDecodeError:
                pass  # we wrote these — defensive only
        ts = [m.get("ts") or "" for m in msgs]
        if ts == sorted(ts):
            continue  # already chronological — leave it alone
        msgs.sort(key=lambda m: m.get("ts") or "")
        path.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs))
        fixed += 1
    return fixed


def _write_index(out_root: Path, index_meta: dict, users: dict, channels: dict) -> dict:
    """Per-channel index.jsonl — one metadata line per thread, anchor-sorted.
    Threads whose every message was corrupt have no `first` → skipped.
    Returns {cid: thread count}."""
    stats: dict = {}
    by_channel: dict = {}
    for (cid, anchor), meta in index_meta.items():
        if "first" not in meta:
            continue  # corrupt thread — nothing written, no index line
        by_channel.setdefault(cid, []).append((anchor, meta))
    for cid, items in by_channel.items():
        items.sort(key=lambda x: x[0])
        stats[cid] = len(items)
        path = out_root / "channels" / cid / "index.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for anchor, meta in items:
                first = meta["first"]
                display, avatar = resolve_author(first, users)
                lr = meta.get("latest_reply")
                entry = {
                    "thread_ts": anchor,
                    "first_ts": meta["first_ts"],
                    "first_user": first.get("user"),
                    "first_author_display": display,
                    "first_author_avatar": avatar,
                    "first_text_preview": make_preview(first.get("text") or "", users, channels),
                    "latest_reply_ts": lr if lr and lr != "0000000000.000000" else meta["first_ts"],
                    "reply_count": first.get("reply_count", 0),
                    "msg_count": meta["msg_count"],
                    "is_thread": bool(meta.get("is_parent")),
                    "has_files": bool(first.get("files")),
                    "participants": (
                        list({first.get("user")} | set(first.get("reply_users") or []))
                        if first.get("user") else []
                    ),
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return stats


def split(conn: sqlite3.Connection, out_root: Path) -> dict:
    """slackdump.sqlite → data/ in three passes. Returns {cid: thread count}.

    Writes users.json, channels.json, per-thread jsonl (ts-ordered) and
    per-channel index.jsonl under out_root.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    users = _load_users(conn)
    channels = _load_channels(conn, users)
    t1 = time.monotonic()
    keep_chunk = _pick_latest(conn)
    t2 = time.monotonic()
    index_meta, written = _stream_split(conn, out_root, users, keep_chunk)  # also seeds bot users
    t3 = time.monotonic()
    fixed = _sort_unordered(written)
    t4 = time.monotonic()
    (out_root / "users.json").write_text(json.dumps(users, ensure_ascii=False, indent=2))
    (out_root / "channels.json").write_text(json.dumps(channels, ensure_ascii=False, indent=2))
    stats = _write_index(out_root, index_meta, users, channels)
    t5 = time.monotonic()
    print(
        f"[splitter] users+channels {t1 - t0:.1f}s · dedup-scan {t2 - t1:.1f}s · "
        f"stream-split {t3 - t2:.1f}s ({len(written)} files) · "
        f"tidy {t4 - t3:.1f}s ({fixed} rewritten) · index {t5 - t4:.1f}s · "
        f"TOTAL {t5 - t0:.1f}s",
        flush=True,
    )
    return stats


def main():
    ap = argparse.ArgumentParser(description="slackdump.sqlite → jsonl splitter")
    ap.add_argument("sqlite_path", type=Path, help="path to slackdump.sqlite")
    ap.add_argument("-o", "--out", type=Path, default=Path("./data"), help="output root")
    args = ap.parse_args()

    conn = sqlite3.connect(args.sqlite_path)
    print(f"reading {args.sqlite_path}")
    stats = split(conn, args.out)
    conn.close()

    total = sum(stats.values())
    print(f"✅ {total} thread jsonl + {len(stats)} channel index across {len(stats)} channels")
    for cid, n in sorted(stats.items()):
        print(f"   {cid}: {n} threads")


if __name__ == "__main__":
    main()

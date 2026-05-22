#!/usr/bin/env python3
"""slackdump.sqlite → data/ : per-thread jsonl + per-channel index.jsonl.

Reuses slackdump's archived SQLite — no re-collection.

Three passes, none with ORDER BY (no sqlite sort temp file):

  1. Light pass — CHANNEL_ID/TS/CHUNK_ID/LOAD_DTTM only (never the DATA
     blob). slackdump stores one message under several chunks; pick the
     newest-LOAD_DTTM chunk per (channel, ts) for dedup.
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
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path


# Slack inline syntax — stripped when building previews.
_USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHAN_RE = re.compile(r"<#([C][A-Z0-9]+)(?:\|([^>]+))?>")
_LINK_LABEL_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
_LINK_BARE_RE = re.compile(r"<(https?://[^>\s]+)>")
_BROADCAST_RE = re.compile(r"<!(here|channel|everyone)>")
# bot_add events embed "<https://.../services/B...|Name>".
_BOT_ADD_RE = re.compile(r"/services/(B[A-Z0-9]+)\|([^>]+)>")


def resolve_author(msg: dict, users: dict) -> tuple[str, str | None]:
    """Display name + avatar for a message, with bot-message fallbacks.

    Priority:
      1. msg.user → users.json
      2. msg.bot_profile.{name, icons}  (embedded in Slack API payload)
      3. msg.bot_id → users.json  (bot entries seeded from bot_add events)
      4. msg.username  (legacy bot field)
      5. msg.attachments[0].{author_name, service_name, footer}
      6. msg.bot_id / msg.user  (last-resort, beats "(unknown)")
    """
    uid = msg.get("user")
    if uid and (u := users.get(uid)):
        name = u.get("display_name") or u.get("real_name") or u.get("name") or uid
        return name, u.get("image_48") or u.get("image_72")

    bp = msg.get("bot_profile") or {}
    if bp.get("name"):
        icons = bp.get("icons") or {}
        return bp["name"], icons.get("image_48") or icons.get("image_72")

    bid = msg.get("bot_id")
    if bid and (b := users.get(bid)):
        name = b.get("display_name") or b.get("real_name") or b.get("name") or bid
        return name, b.get("image_48") or b.get("image_72")

    if msg.get("username"):
        return msg["username"], None

    for a in (msg.get("attachments") or [])[:1]:
        for key in ("author_name", "service_name", "footer"):
            if a.get(key):
                return a[key], a.get("author_icon") or a.get("service_icon")

    if bid:
        return f"bot:{bid}", None
    if uid:
        return uid, None
    return "(unknown)", None


def make_preview(text: str, users: dict | None = None, channels: dict | None = None,
                 max_len: int = 100) -> str:
    """Strip Slack syntax + truncate to max_len. Resolves mention display names
    (when a users dict is given). Stripping happens before truncation so the
    preview never ends on a dangling `<` that would break HTML parsing."""
    if not text:
        return ""
    users = users or {}
    channels = channels or {}

    def user_repl(m):
        uid, alias = m.group(1), m.group(2)
        if alias:
            return "@" + alias
        u = users.get(uid) or {}
        return "@" + (u.get("display_name") or u.get("real_name") or u.get("name") or uid)

    def chan_repl(m):
        cid, alias = m.group(1), m.group(2)
        if alias:
            return "#" + alias
        c = channels.get(cid) or {}
        return "#" + (c.get("name") or cid)

    text = _USER_RE.sub(user_repl, text)
    text = _CHAN_RE.sub(chan_repl, text)
    text = _LINK_LABEL_RE.sub(lambda m: m.group(2), text)
    text = _LINK_BARE_RE.sub(lambda m: m.group(1), text)
    text = _BROADCAST_RE.sub(lambda m: "@" + m.group(1), text)
    text = text.replace("\n", " ").strip()
    return text[:max_len]


def _parse(data) -> dict | None:
    """Decode + json.loads one DATA blob; None on a corrupt blob."""
    try:
        if isinstance(data, bytes):
            data = data.decode()
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _load_users(conn: sqlite3.Connection) -> dict:
    """S_USER table → {uid: profile}. Per-integration bot identities are added
    later, during the streaming pass, from MESSAGE blobs."""
    users = {}
    for (data,) in conn.execute("SELECT DATA FROM S_USER"):
        u = _parse(data)
        if not u or not u.get("id"):
            continue
        profile = u.get("profile") or {}
        users[u["id"]] = {
            "name": u.get("name"),
            "real_name": u.get("real_name"),
            "display_name": profile.get("display_name") or profile.get("display_name_normalized"),
            "is_bot": u.get("is_bot"),
            "deleted": u.get("deleted"),
            "image_24": profile.get("image_24"),
            "image_48": profile.get("image_48"),
            "image_72": profile.get("image_72"),
            "image_192": profile.get("image_192"),
        }
    return users


def _load_channels(conn: sqlite3.Connection, users: dict) -> dict:
    """CHANNEL table → {cid: meta}. DMs/MPIMs have no name — derive one."""
    channels = {}
    for (data,) in conn.execute("SELECT DATA FROM CHANNEL"):
        c = _parse(data)
        if not c or not c.get("id"):
            continue
        name = c.get("name") or c.get("name_normalized")
        other_uid = None
        if not name and c.get("is_im"):
            other_uid = c.get("user")  # IM's .user is the other party's uid
            if other_uid:
                u = users.get(other_uid) or {}
                name = u.get("display_name") or u.get("real_name") or other_uid
        if not name and c.get("is_mpim"):
            members = c.get("members") or []
            names = [users.get(m, {}).get("display_name") or m for m in members[:3]]
            name = f"mpim: {','.join(names)}"
        channels[c["id"]] = {
            "name": name or c["id"],
            "is_im": c.get("is_im"),
            "is_mpim": c.get("is_mpim"),
            "is_private": c.get("is_private"),
            "is_channel": c.get("is_channel"),
            "is_archived": c.get("is_archived"),
            "other_uid": other_uid,
            "members": c.get("members") or [],
        }
    return channels


def _collect_bot(msg: dict, users: dict) -> None:
    """Seed a bot identity from a message's bot_profile / bot_add text — only
    when a name is available, so a nameless placeholder can't block a later
    named source."""
    bp = msg.get("bot_profile") or {}
    bp_name = bp.get("name")
    bid = bp.get("id") or msg.get("bot_id")
    if bid and bp_name and bid not in users:
        icons = bp.get("icons") or {}
        users[bid] = {
            "name": bp_name, "real_name": bp_name, "display_name": bp_name,
            "is_bot": True,
            "image_48": icons.get("image_48"), "image_72": icons.get("image_72"),
        }
    if msg.get("subtype") == "bot_add":
        mm = _BOT_ADD_RE.search(msg.get("text") or "")
        if mm:
            bid2, name = mm.group(1), mm.group(2)
            existing = users.get(bid2) or {}
            if not existing.get("name"):
                users[bid2] = {
                    "name": name, "real_name": name, "display_name": name,
                    "is_bot": True,
                    "image_48": existing.get("image_48"), "image_72": existing.get("image_72"),
                }


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


def _pick_latest(conn: sqlite3.Connection) -> dict:
    """Pass 1 — pick the CHUNK_ID to keep per (channel, ts): newest LOAD_DTTM.

    Reads no DATA blob, so it stays cheap even on a large archive.
    """
    best: dict = {}  # (cid, ts) → (load_dttm, chunk_id)
    for cid, ts, chunk_id, load in conn.execute(
        "SELECT CHANNEL_ID, TS, CHUNK_ID, LOAD_DTTM FROM MESSAGE"
    ):
        k = (cid, ts)
        cur = best.get(k)
        if cur is None or load >= cur[0]:
            best[k] = (load, chunk_id)
    return {k: v[1] for k, v in best.items()}


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

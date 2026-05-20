#!/usr/bin/env python3
"""
slackdump.sqlite → 每 thread 一份 jsonl + channel index.jsonl

不重写采集，复用 slackdump archive 已经拉好的 SQLite。
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from tqdm import tqdm


# 剥掉 Slack 特殊语法（<@U..>, <#C..>, <url>, <!here> 等），保留人类可读字符
# 在生成 preview 前调用，避免截断在 `<url>` 中间导致 HTML 渲染时把 `<` 当 tag 起点
_USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHAN_RE = re.compile(r"<#([C][A-Z0-9]+)(?:\|([^>]+))?>")
_LINK_LABEL_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
_LINK_BARE_RE = re.compile(r"<(https?://[^>\s]+)>")
_BROADCAST_RE = re.compile(r"<!(here|channel|everyone)>")


def make_preview(text: str, users: dict | None = None, channels: dict | None = None, max_len: int = 100) -> str:
    """剥 Slack 特殊语法 + 截 100 字。mention 解析 display_name（如有 users dict）。"""
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


def split_threads(conn: sqlite3.Connection, out_root: Path) -> dict:
    """每 thread 一份 jsonl，按 ts 升序。返回每 channel 的 thread 数统计。"""
    stats = {}
    channels = [r[0] for r in conn.execute("SELECT DISTINCT CHANNEL_ID FROM MESSAGE")]

    for cid in tqdm(channels, desc="split", unit="ch"):
        threads_dir = out_root / "channels" / cid / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)

        # 一个 thread 的 anchor ts = THREAD_TS (有 thread 时) 或 TS (没 thread 的孤儿消息)
        thread_anchors = [
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT COALESCE(THREAD_TS, TS) AS anchor
                FROM MESSAGE
                WHERE CHANNEL_ID = ?
                ORDER BY anchor
                """,
                (cid,),
            )
        ]

        for ts in thread_anchors:
            # MESSAGE 表主键是 (TS, CHUNK_ID)，同一条消息可能在 type=0 (channel
            # history) 和 type=1 (thread) 两个 chunk 里各存一份。按 TS 去重，
            # 拿 LOAD_DTTM 最新那份（覆盖编辑场景）。
            rows = conn.execute(
                """
                SELECT DATA FROM MESSAGE m
                WHERE CHANNEL_ID = ?
                  AND (THREAD_TS = ? OR (THREAD_TS IS NULL AND TS = ?))
                  AND LOAD_DTTM = (
                      SELECT MAX(LOAD_DTTM) FROM MESSAGE m2
                      WHERE m2.CHANNEL_ID = m.CHANNEL_ID AND m2.TS = m.TS
                  )
                GROUP BY TS
                ORDER BY TS
                """,
                (cid, ts, ts),
            ).fetchall()
            # Decode each row; skip ones with corrupt DATA blobs so a single
            # bad message can't take down the whole thread (or whole splitter).
            parsed = []
            for (data,) in rows:
                try:
                    if isinstance(data, bytes):
                        data = data.decode()
                    parsed.append(json.loads(data))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    print(f"⚠️  {cid}/{ts}: skip corrupt row ({e})", file=sys.stderr)
            if not parsed:
                # All rows in this thread were corrupt → skip writing a stub.
                print(f"⚠️  {cid}/{ts}: no valid messages, skip thread", file=sys.stderr)
                continue
            with open(threads_dir / f"{ts}.jsonl", "w") as f:
                for obj in parsed:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        stats[cid] = len(thread_anchors)

    return stats


def write_index(conn: sqlite3.Connection, out_root: Path) -> None:
    """每 channel 一个 index.jsonl，每行一个 thread 的 metadata。"""
    # 加载 users + channels metadata 用于 preview 解析 mention
    users = json.loads((out_root / "users.json").read_text()) if (out_root / "users.json").exists() else {}
    channels_meta = json.loads((out_root / "channels.json").read_text()) if (out_root / "channels.json").exists() else {}

    channels = [r[0] for r in conn.execute("SELECT DISTINCT CHANNEL_ID FROM MESSAGE")]

    for cid in channels:
        # 拉每个 thread anchor 的元数据
        # 对于真 thread (IS_PARENT=1)，从父帖拿 reply_count / latest_reply
        # 对于孤儿消息，自身就是唯一的 message
        rows = conn.execute(
            """
            WITH thread_anchors AS (
                SELECT DISTINCT COALESCE(THREAD_TS, TS) AS anchor
                FROM MESSAGE
                WHERE CHANNEL_ID = ?
            ),
            -- 去重：同一 TS 可能在多个 chunk 里，取最新 LOAD_DTTM 的
            dedup_msg AS (
                SELECT m.* FROM MESSAGE m
                WHERE m.CHANNEL_ID = ?
                  AND m.LOAD_DTTM = (
                      SELECT MAX(LOAD_DTTM) FROM MESSAGE m2
                      WHERE m2.CHANNEL_ID = m.CHANNEL_ID AND m2.TS = m.TS
                  )
            )
            SELECT
                ta.anchor AS thread_ts,
                m.TS AS first_ts,
                m.DATA AS first_data,
                m.IS_PARENT,
                m.LATEST_REPLY,
                (
                    SELECT COUNT(DISTINCT TS) FROM MESSAGE m2
                    WHERE m2.CHANNEL_ID = ?
                      AND (m2.THREAD_TS = ta.anchor OR (m2.THREAD_TS IS NULL AND m2.TS = ta.anchor))
                ) AS msg_count
            FROM thread_anchors ta
            LEFT JOIN dedup_msg m ON m.TS = ta.anchor
            GROUP BY ta.anchor
            ORDER BY ta.anchor
            """,
            (cid, cid, cid),
        ).fetchall()

        index_path = out_root / "channels" / cid / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)

        with open(index_path, "w") as f:
            for ts, first_ts, first_data, is_parent, latest_reply, msg_count in rows:
                # Same error-recovery contract as split_threads: a single
                # corrupt row should not stop the whole channel index.
                if first_data is None:
                    print(f"⚠️  {cid}/{ts}: index skip (no first_data)", file=sys.stderr)
                    continue
                try:
                    raw = first_data.decode() if isinstance(first_data, bytes) else first_data
                    first = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    print(f"⚠️  {cid}/{ts}: index skip corrupt row ({e})", file=sys.stderr)
                    continue
                text_preview = make_preview(first.get("text") or "", users, channels_meta)
                entry = {
                    "thread_ts": ts,
                    "first_ts": first_ts,
                    "first_user": first.get("user"),
                    "first_text_preview": text_preview,
                    "latest_reply_ts": latest_reply if latest_reply and latest_reply != "0000000000.000000" else first_ts,
                    "reply_count": first.get("reply_count", 0),
                    "msg_count": msg_count,
                    "is_thread": bool(is_parent),
                    "has_files": bool(first.get("files")),
                    "participants": list({first.get("user")} | set(first.get("reply_users") or [])) if first.get("user") else [],
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_users_and_channels(conn: sqlite3.Connection, out_root: Path) -> None:
    """从 S_USER / CHANNEL 表派生 users.json + channels.json，render 解析 ID 用。"""
    # users.json：uid → {name, real_name, display_name}
    users = {}
    for (data,) in conn.execute("SELECT DATA FROM S_USER"):
        u = json.loads(data.decode() if isinstance(data, bytes) else data)
        uid = u.get("id")
        if not uid:
            continue
        profile = u.get("profile") or {}
        users[uid] = {
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
    (out_root / "users.json").write_text(json.dumps(users, ensure_ascii=False, indent=2))

    # channels.json：cid → {name, is_im/mpim/channel/private, members?}
    channels = {}
    for (data,) in conn.execute("SELECT DATA FROM CHANNEL"):
        c = json.loads(data.decode() if isinstance(data, bytes) else data)
        cid = c.get("id")
        if not cid:
            continue
        # DM 没有 name，按对方 user 推
        name = c.get("name") or c.get("name_normalized")
        other_uid = None
        if not name and c.get("is_im"):
            other_uid = c.get("user")  # IM 的 .user 是对方 uid
            if other_uid:
                u = users.get(other_uid) or {}
                name = u.get("display_name") or u.get("real_name") or other_uid
        if not name and c.get("is_mpim"):
            members = c.get("members") or []
            names = [users.get(m, {}).get("display_name") or m for m in members[:3]]
            name = f"mpim: {','.join(names)}"
        channels[cid] = {
            "name": name or cid,
            "is_im": c.get("is_im"),
            "is_mpim": c.get("is_mpim"),
            "is_private": c.get("is_private"),
            "is_channel": c.get("is_channel"),
            "is_archived": c.get("is_archived"),
            "other_uid": other_uid,  # IM 对方 uid，用于渲染头像
            "members": c.get("members") or [],
        }
    (out_root / "channels.json").write_text(json.dumps(channels, ensure_ascii=False, indent=2))
    print(f"  users.json: {len(users)} users / channels.json: {len(channels)} channels")


def main():
    ap = argparse.ArgumentParser(description="slackdump.sqlite → jsonl splitter")
    ap.add_argument("sqlite_path", type=Path, help="path to slackdump.sqlite")
    ap.add_argument("-o", "--out", type=Path, default=Path("./data"), help="output root (default ./data)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.sqlite_path)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"reading {args.sqlite_path}")
    write_users_and_channels(conn, args.out)
    stats = split_threads(conn, args.out)
    write_index(conn, args.out)

    total_threads = sum(stats.values())
    print(f"✅ wrote {total_threads} thread jsonl + {len(stats)} channel index across {len(stats)} channels:")
    for cid, n in stats.items():
        print(f"   {cid}: {n} threads")


if __name__ == "__main__":
    main()

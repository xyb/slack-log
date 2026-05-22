"""Read slackdump's archive SQLite — the parts every profile needs.

slackdump stores one message under several chunks (channel-history chunk +
thread-replies chunk), so a (channel, ts) pair can appear more than once. The
helpers here own that dedup, plus user / channel / bot identity decoding, so
the splitter, the indexer and any future reader all agree on it.
"""

import json
import re
import sqlite3

# bot_add events embed "<https://.../services/B...|Name>".
_BOT_ADD_RE = re.compile(r"/services/(B[A-Z0-9]+)\|([^>]+)>")


def _parse(data) -> dict | None:
    """Decode + json.loads one DATA blob; None on a corrupt blob."""
    try:
        if isinstance(data, bytes):
            data = data.decode()
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


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


def _pick_latest(conn: sqlite3.Connection) -> dict:
    """Pick the CHUNK_ID to keep per (channel, ts): newest LOAD_DTTM.

    Reads no DATA blob, so it stays cheap even on a large archive. slackdump
    stores the same message under several chunks; the newest load wins (which
    also makes an edited message supersede its earlier copy).
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

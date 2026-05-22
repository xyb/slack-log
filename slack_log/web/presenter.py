"""Presenter — turn raw archive data into template-ready context.

Both the FastAPI server (web/app.py) and the static-HTML exporter
(web/static_export.py) render the same Jinja templates, so the "raw message /
thread dict → the `_`-prefixed fields a template needs" step lives here, shared.

It takes plain dicts in and returns plain dicts out — it never reads a file or
a database; that is the store's job.
"""

from datetime import datetime
from pathlib import Path

from slack_log.core.slackdump_db import resolve_author
from slack_log.core.text import emojize, expand_for_preview, expand_mentions


def ts_to_human(ts: str) -> str:
    """Slack ts (unix) → 人类可读时间。"""
    try:
        dt = datetime.fromtimestamp(float(ts))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ts


def render_user(uid: str | None, users: dict) -> str:
    if not uid:
        return "(unknown)"
    u = users.get(uid) or {}
    return u.get("display_name") or u.get("real_name") or u.get("name") or uid


def render_channel(cid: str, channels: dict) -> str:
    c = channels.get(cid) or {}
    return c.get("name") or cid


def kind_of(cid: str, channels_meta: dict) -> str:
    """channel / dm / mpim"""
    c = channels_meta.get(cid) or {}
    if c.get("is_im"):
        return "dm"
    if c.get("is_mpim"):
        return "mpim"
    return "channel"


def format_bytes(n: int) -> str:
    if not n:
        return "?"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            num = f"{n:.1f}".rstrip("0").rstrip(".")
            return f"{num} {unit}"
        n /= 1024
    num = f"{n:.1f}".rstrip("0").rstrip(".")
    return f"{num} TB"


def render_attachments(attachments: list, users: dict, channels: dict) -> list:
    """Slack 链接 unfurl 卡片 → 渲染数据。

    一条 attachment 含 service_name/title/text/image_url/thumb_url/color 等,
    Slack 把 https://github.com/... 这种链接自动 unfurl 成卡片附在消息后。
    """
    out = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        color = a.get("color")
        if color and not color.startswith("#"):
            color = "#" + color
        out.append({
            "color": color or "#dfe2e6",
            "service_name": a.get("service_name"),
            "service_icon": a.get("service_icon"),
            "title": a.get("title"),
            "title_link": a.get("title_link") or a.get("from_url") or a.get("original_url"),
            "text_html": expand_mentions(a.get("text") or "", users, channels),
            "image_url": a.get("image_url"),
            "thumb_url": a.get("thumb_url"),
            "author_name": a.get("author_name"),
            "author_icon": a.get("author_icon"),
            "author_link": a.get("author_link"),
            "footer": a.get("footer"),
            "from_url": a.get("from_url") or a.get("original_url"),
        })
    return out


def render_files(files: list, att_dir: Path) -> list:
    """每个文件返回 dict: kind=image|link|remote。

    att_dir 是该 channel 的 attachments 目录（attach.py 的下载产物）。
    本地已下载就用相对路径 ../attachments/<id>.<ext> 引用（HTML 在 channels/<cid>
    下,符号链接到 attachments）；未下载就指向 Slack permalink（点击跳 Slack 拿）。
    """
    out = []
    for f in files:
        fid = f.get("id")
        if not fid:
            continue
        name = f.get("name") or fid
        mimetype = f.get("mimetype", "")
        size = f.get("size", 0)
        ft = f.get("filetype") or "bin"
        local_file = att_dir / f"{fid}.{ft}"

        permalink = f.get("permalink") or f.get("url_private_download") or f.get("url_private") or ""
        size_human = format_bytes(size)

        if local_file.exists():
            rel = f"../attachments/{fid}.{ft}"
            entry = {
                "name": name, "mimetype": mimetype, "size": size, "size_human": size_human,
                "rel": rel, "fid": fid, "ext": ft, "permalink": permalink,
                "kind": "image" if mimetype.startswith("image/") else "link",
            }
            out.append(entry)
        else:
            out.append({
                "name": name, "mimetype": mimetype, "size": size, "size_human": size_human,
                "kind": "remote", "href": permalink, "fid": fid,
            })
    return out


def enrich_messages(msgs: list[dict], users: dict, channels: dict, att_dir: Path) -> list[dict]:
    """Attach the _-prefixed render fields each message needs for thread.html.
    att_dir is the channel's attachments directory (see render_files)."""
    for m in msgs:
        m["_ref_id"] = f"msg-{m['ts']}"
        m["_human_time"] = ts_to_human(m["ts"])
        display, avatar = resolve_author(m, users)
        m["_user_display"] = display
        m["_avatar"] = avatar
        m["_text_rendered"] = expand_mentions(m.get("text") or "", users, channels)
        m["_reactions_rendered"] = [
            {
                "emoji": emojize(f":{r['name']}:"),
                "name": r.get("name"),
                "count": r.get("count", 0),
                "users": [
                    {
                        "uid": uid,
                        "display": render_user(uid, users),
                        "avatar": (users.get(uid) or {}).get("image_24")
                        or (users.get(uid) or {}).get("image_48"),
                    }
                    for uid in (r.get("users") or [])
                ],
            }
            for r in (m.get("reactions") or [])
        ]
        m["_files_rendered"] = render_files(m.get("files") or [], att_dir)
        m["_attachments_rendered"] = render_attachments(m.get("attachments") or [], users, channels)
    return msgs


def enrich_thread_meta(threads_meta: list[dict], users: dict, channels: dict) -> list[dict]:
    """Attach the _-prefixed fields channel_index.html needs to each thread row."""
    for tm in threads_meta:
        tm["_first_human"] = ts_to_human(tm["first_ts"])
        tm["_latest_human"] = ts_to_human(tm["latest_reply_ts"])
        tm["_first_user_display"] = tm.get("first_author_display") or render_user(
            tm.get("first_user"), users
        )
        tm["_first_user_avatar"] = tm.get("first_author_avatar")
        if not tm["_first_user_avatar"] and tm.get("first_user"):
            uobj = users.get(tm["first_user"]) or {}
            tm["_first_user_avatar"] = uobj.get("image_48") or uobj.get("image_72")
        tm["_preview_rendered"] = expand_for_preview(tm.get("first_text_preview") or "", users, channels)
    return threads_meta

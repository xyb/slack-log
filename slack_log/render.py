#!/usr/bin/env python3
"""
data/ → html/ 静态 HTML 渲染（jinja2）

- 每 channel 一个 index.html：thread 列表，两种排序（first_ts / latest_reply_ts）
- 每 thread 一个 thread.html：IRC log 风格 + 每条消息 <a id="msg-<ts>"> ref id 永久锚点
- 全局 index.html：channel 列表入口

外部稳定引用格式：html/channels/<cid>/threads/<thread_ts>.html#msg-<ts>

Slack 文本处理（mention / mrkdwn / emoji 展开）在 core.text。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from tqdm import tqdm

from slack_log.core.slackdump_db import resolve_author
from slack_log.core.text import emojize, expand_for_preview, expand_mentions


def ts_to_human(ts: str) -> str:
    """Slack ts (unix) → 人类可读时间。"""
    try:
        dt = datetime.fromtimestamp(float(ts))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ts


def load_users(data_root: Path) -> dict:
    p = data_root / "users.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def load_channels(data_root: Path) -> dict:
    p = data_root / "channels.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def render_user(uid: str | None, users: dict) -> str:
    if not uid:
        return "(unknown)"
    u = users.get(uid) or {}
    return u.get("display_name") or u.get("real_name") or u.get("name") or uid


def render_channel(cid: str, channels: dict) -> str:
    c = channels.get(cid) or {}
    return c.get("name") or cid


def load_thread(thread_jsonl: Path) -> list[dict]:
    msgs = []
    with open(thread_jsonl) as f:
        for line in f:
            line = line.strip()
            if line:
                msgs.append(json.loads(line))
    return msgs


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

    一条 attachment 含 service_name/title/text/image_url/thumb_url/color 等，
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
    下，符号链接到 attachments）；未下载就指向 Slack permalink（点击跳 Slack 拿）。
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
    Shared by static render (render.py) and dynamic render (server.py).
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


def load_thread_meta(channel_dir: Path) -> list[dict]:
    """Read a channel's index.jsonl into a list of thread-meta dicts."""
    index_path = channel_dir / "index.jsonl"
    out: list[dict] = []
    if index_path.exists():
        with open(index_path) as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    return out


def build_global_groups(data_root: Path, channels_meta: dict, users: dict,
                        include: set | None = None) -> dict:
    """Build the {channels, dms, mpims} groups for global_index.html."""
    include = include or {"channel", "dm", "mpim"}
    real_channels, dms, mpims = [], [], []
    for cdir in (data_root / "channels").iterdir():
        if not cdir.is_dir():
            continue
        cid = cdir.name
        kind = kind_of(cid, channels_meta)
        if kind not in include:
            continue
        n_threads = len(list((cdir / "threads").glob("*.jsonl"))) if (cdir / "threads").exists() else 0
        cinfo = channels_meta.get(cid) or {}
        item = {"id": cid, "name": cinfo.get("name") or cid, "thread_count": n_threads}
        if cinfo.get("is_im"):
            other_uid = cinfo.get("other_uid")
            other_user = users.get(other_uid) if other_uid else None
            item["avatar"] = (other_user or {}).get("image_72") if other_user else None
            dms.append(item)
        elif cinfo.get("is_mpim"):
            members = cinfo.get("members") or []
            item["avatars"] = [
                (users.get(m) or {}).get("image_48")
                for m in members[:5]
                if (users.get(m) or {}).get("image_48")
            ]
            mpims.append(item)
        else:
            real_channels.append(item)
    real_channels.sort(key=lambda c: c["name"])
    dms.sort(key=lambda c: c["thread_count"], reverse=True)
    mpims.sort(key=lambda c: c["thread_count"], reverse=True)
    return {"channels": real_channels, "dms": dms, "mpims": mpims}


def render_channel_html(channel_dir: Path, html_root: Path, users: dict, channels: dict, env: Environment, generated_at: str) -> None:
    cid = channel_dir.name
    channel_name = render_channel(cid, channels)
    out_dir = html_root / "channels" / cid
    threads_out = out_dir / "threads"
    threads_out.mkdir(parents=True, exist_ok=True)

    # symlink data 的 attachments 到 html，让 HTML 用相对路径 ../attachments/ 引用
    data_att = channel_dir / "attachments"
    html_att = out_dir / "attachments"
    if data_att.exists() and not html_att.exists():
        html_att.symlink_to(data_att.resolve())

    threads_meta = load_thread_meta(channel_dir)

    # 渲染每个 thread.html — 单 thread 失败不影响其他 thread / channel index
    for tm in threads_meta:
        ttp = channel_dir / "threads" / f"{tm['thread_ts']}.jsonl"
        if not ttp.exists():
            continue
        try:
            msgs = enrich_messages(load_thread(ttp), users, channels, channel_dir / "attachments")
        except Exception as e:
            print(f"⚠️  {cid}/{tm['thread_ts']}: load failed ({type(e).__name__}: {e}) — skip", file=sys.stderr)
            continue
        try:
            html = env.get_template("thread.html").render(
                channel_id=cid,
                channel_name=channel_name,
                thread_meta=tm,
                messages=msgs,
                generated_at=generated_at,
            )
            (threads_out / f"{tm['thread_ts']}.html").write_text(html, encoding="utf-8")
        except Exception as e:
            print(f"⚠️  {cid}/{tm['thread_ts']}: render failed ({type(e).__name__}: {e}) — skip", file=sys.stderr)
            continue

    enrich_thread_meta(threads_meta, users, channels)
    html = env.get_template("channel_index.html").render(
        channel_id=cid,
        channel_name=channel_name,
        threads=threads_meta,
        generated_at=generated_at,
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def render_global(data_root: Path, html_root: Path, channels_meta: dict, users: dict, env: Environment, generated_at: str, include: set | None = None) -> None:
    """全局 index.html：分三组（channel / DM / MPIM）+ DM 显示对方头像。

    include 集合过滤要显示的类型，默认 {channel, dm, mpim} 全部。
    """
    groups = build_global_groups(data_root, channels_meta, users, include=include)
    html = env.get_template("global_index.html").render(
        **groups,
        generated_at=generated_at,
    )
    (html_root / "index.html").write_text(html, encoding="utf-8")


def kind_of(cid: str, channels_meta: dict) -> str:
    """channel / dm / mpim"""
    c = channels_meta.get(cid) or {}
    if c.get("is_im"):
        return "dm"
    if c.get("is_mpim"):
        return "mpim"
    return "channel"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("./data"))
    ap.add_argument("--html", type=Path, default=Path("./html"))
    ap.add_argument("--templates", type=Path, default=Path(__file__).parent / "templates")
    ap.add_argument("--flavor", choices=("server", "static"), default="server",
                    help="server: no-suffix absolute URLs (FastAPI). static: relative .html links (http.server).")
    ap.add_argument(
        "--include",
        default="channel,dm,mpim",
        help="逗号分隔，选 channel / dm / mpim 任意组合（默认全部）。例：--include=channel 只渲染真频道",
    )
    args = ap.parse_args()

    include = {s.strip() for s in args.include.split(",") if s.strip()}
    valid = {"channel", "dm", "mpim"}
    if not include.issubset(valid):
        raise SystemExit(f"--include 只接受 {valid} 的子集，收到 {include}")

    # Pick template flavor:
    #   --flavor server (default) → no-suffix absolute URLs, for the v0.7 FastAPI server
    #   --flavor static           → relative .html links, for `python3 -m http.server`
    templates_root = args.templates
    flavor = args.flavor
    if (templates_root / flavor).is_dir():
        templates_root = templates_root / flavor

    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=select_autoescape(["html"]),
    )

    users = load_users(args.data)
    channels_meta = load_channels(args.data)
    args.html.mkdir(exist_ok=True)
    (args.html / "channels").mkdir(exist_ok=True)

    # Epoch strings — the browser renders them in the visitor's local timezone.
    generated_at = str(datetime.now().timestamp())
    fetched_at = ""
    for cand in (Path("raw/slackdump.sqlite"), args.data / "users.json"):
        if cand.exists():
            fetched_at = str(cand.stat().st_mtime)
            break
    env.globals["fetched_at"] = fetched_at

    candidates = [
        c for c in (args.data / "channels").iterdir()
        if c.is_dir() and kind_of(c.name, channels_meta) in include
    ]
    skipped = sum(1 for c in (args.data / "channels").iterdir()
                  if c.is_dir() and kind_of(c.name, channels_meta) not in include)

    for cdir in tqdm(candidates, desc="render", unit="ch"):
        render_channel_html(cdir, args.html, users, channels_meta, env, generated_at)

    render_global(args.data, args.html, channels_meta, users, env, generated_at, include)
    print(f"\n✅ html/ 生成完毕：{args.html}")
    print(f"   include={sorted(include)} / 跳过 {skipped} 个不在 include 的")
    print(f"   generated at: {generated_at}")
    print(f"   入口：{args.html / 'index.html'}")


if __name__ == "__main__":
    main()

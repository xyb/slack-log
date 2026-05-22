#!/usr/bin/env python3
"""
Static-HTML exporter — data/ → html/ (jinja2).

The no-backend deploy: emits a plain HTML tree you open with file:// or drop on
any static host. The FastAPI server renders the same templates dynamically; the
difference is the `static` template flavor (relative .html links).

- one index.html per channel: the thread list, two sort orders
- one thread.html per thread: IRC-log style + a permanent <a id="msg-<ts>">
- a global index.html: the channel-list entry point

Reads through a JsonlStore and turns rows into template context via the shared
presenter — the same two pieces the server is built from.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from tqdm import tqdm

from slack_log.store import JsonlStore
from slack_log.web import presenter


def render_channel_html(cid: str, store: JsonlStore, html_root: Path,
                        env: Environment, generated_at: str) -> None:
    channel_name = presenter.render_channel(cid, store.channels())
    out_dir = html_root / "channels" / cid
    threads_out = out_dir / "threads"
    threads_out.mkdir(parents=True, exist_ok=True)

    # symlink data 的 attachments 到 html,让 HTML 用相对路径 ../attachments/ 引用
    data_att = store.attachments_dir(cid)
    html_att = out_dir / "attachments"
    if data_att.exists() and not html_att.exists():
        html_att.symlink_to(data_att.resolve())

    threads_meta = store.thread_meta(cid)

    # 渲染每个 thread.html — 单 thread 失败不影响其他 thread / channel index
    for tm in threads_meta:
        try:
            raw = store.load_thread(cid, tm["thread_ts"])
            if raw is None:
                continue
            msgs = presenter.enrich_messages(
                raw, store.users(), store.channels(), store.attachments_dir(cid))
        except Exception as e:
            print(f"⚠️  {cid}/{tm['thread_ts']}: load failed ({type(e).__name__}: {e}) — skip",
                  file=sys.stderr)
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
            print(f"⚠️  {cid}/{tm['thread_ts']}: render failed ({type(e).__name__}: {e}) — skip",
                  file=sys.stderr)
            continue

    presenter.enrich_thread_meta(threads_meta, store.users(), store.channels())
    html = env.get_template("channel_index.html").render(
        channel_id=cid,
        channel_name=channel_name,
        threads=threads_meta,
        generated_at=generated_at,
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def render_global(store: JsonlStore, html_root: Path, env: Environment,
                  generated_at: str, include: set | None = None) -> None:
    """全局 index.html：分三组（channel / DM / MPIM）+ DM 显示对方头像。"""
    groups = store.global_groups(include=include)
    html = env.get_template("global_index.html").render(**groups, generated_at=generated_at)
    (html_root / "index.html").write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Export a Slack archive as static HTML")
    ap.add_argument("--data", type=Path, default=Path("./data"))
    ap.add_argument("--html", type=Path, default=Path("./html"))
    ap.add_argument("--templates", type=Path,
                    default=Path(__file__).parent.parent / "templates")
    ap.add_argument("--flavor", choices=("server", "static"), default="server",
                    help="server: no-suffix absolute URLs (FastAPI). static: relative .html links.")
    ap.add_argument(
        "--include",
        default="channel,dm,mpim",
        help="逗号分隔,选 channel / dm / mpim 任意组合（默认全部）。例：--include=channel 只渲染真频道",
    )
    args = ap.parse_args()

    include = {s.strip() for s in args.include.split(",") if s.strip()}
    valid = {"channel", "dm", "mpim"}
    if not include.issubset(valid):
        raise SystemExit(f"--include 只接受 {valid} 的子集,收到 {include}")

    # Pick template flavor:
    #   --flavor server (default) → no-suffix absolute URLs, for the FastAPI server
    #   --flavor static           → relative .html links, for `python3 -m http.server`
    templates_root = args.templates
    if (templates_root / args.flavor).is_dir():
        templates_root = templates_root / args.flavor

    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=select_autoescape(["html"]),
    )

    store = JsonlStore(data_root=args.data)
    args.html.mkdir(exist_ok=True)
    (args.html / "channels").mkdir(exist_ok=True)

    # Epoch strings — the browser renders them in the visitor's local timezone.
    generated_at = str(datetime.now().timestamp())
    env.globals["fetched_at"] = store.fetched_at()

    channels_meta = store.channels()
    all_cids = store.list_channels()
    candidates = [c for c in all_cids if presenter.kind_of(c, channels_meta) in include]
    skipped = len(all_cids) - len(candidates)

    for cid in tqdm(candidates, desc="render", unit="ch"):
        render_channel_html(cid, store, args.html, env, generated_at)

    render_global(store, args.html, env, generated_at, include)
    print(f"\n✅ html/ 生成完毕：{args.html}")
    print(f"   include={sorted(include)} / 跳过 {skipped} 个不在 include 的")
    print(f"   generated at: {generated_at}")
    print(f"   入口：{args.html / 'index.html'}")


if __name__ == "__main__":
    main()

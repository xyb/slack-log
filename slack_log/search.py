"""Command-line full-text search over the local Slack archive.

    python3 -m slack_log.search "黑边"
    python3 -m slack_log.search "质检 分辨率" --channel scraping --after 2026-06-01
    python3 -m slack_log.search anamorphic --user xyb --full

Chinese works here. The query is run through the same CJK-splitting FTS
transform the indexer uses (index._to_fts_query → core.text.split_cjk): a
multi-char Chinese word like 黑边 is split to the phrase "黑 边" so unicode61's
per-char tokens match. Raw `sqlite3 search.db "... MATCH '黑边'"` does NOT do this
and silently returns 0 — always search through this CLI or the web UI, never raw
MATCH. Space between words = AND (each word matched as a phrase).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
import sys
from pathlib import Path

from slack_log.core.text import join_cjk
from slack_log.pipeline.index import search

_MARK = re.compile(r"</?mark>")


def _clean(text: str) -> str:
    """Strip the web <mark> highlight tags, then collapse CJK split-spaces.

    The snippet comes from the CJK-split FTS column; join_cjk on the raw snippet
    can't bridge chars separated by a <mark> tag, so drop the tags first, then
    re-join so Chinese reads contiguously in the terminal.
    """
    return join_cjk(_MARK.sub("", text or "")).replace("\n", " ").strip()

# Repo root: <repo>/slack_log/search.py → search.db lives at the repo root.
DEFAULT_DB = Path(__file__).resolve().parent.parent / "search.db"


def _date_to_epoch(date_str: str) -> str:
    """YYYY-MM-DD → epoch-second string (UTC midnight), for ts range filters."""
    d = _dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    return str(int(d.timestamp()))


def _fmt_ts(ts: str) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts or "?"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m slack_log.search",
        description="Chinese-aware full-text search over the local Slack archive (search.db).",
    )
    ap.add_argument("query", help="search terms; space = AND. Chinese words work (黑边 质检).")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to search.db (default: repo root)")
    ap.add_argument("-n", "--limit", type=int, default=20, help="max results (default 20)")
    ap.add_argument("--channel", help="filter by channel name substring")
    ap.add_argument("--user", help="filter by display name substring")
    ap.add_argument("--kind", help="comma list of channel,dm,mpim")
    ap.add_argument("--after", help="only messages on/after this date (YYYY-MM-DD)")
    ap.add_argument("--before", help="only messages before this date (YYYY-MM-DD)")
    ap.add_argument("--full", action="store_true", help="print full message text, not the snippet")
    ap.add_argument("--json", action="store_true", help="emit raw JSON results")
    args = ap.parse_args(argv)

    include = {k.strip() for k in args.kind.split(",") if k.strip()} if args.kind else None
    try:
        after = _date_to_epoch(args.after) if args.after else None
        before = _date_to_epoch(args.before) if args.before else None
    except ValueError:
        ap.error("--after/--before must be YYYY-MM-DD")

    if not Path(args.db).exists():
        ap.error(f"search.db not found at {args.db} — run `make personal-build` first")

    conn = sqlite3.connect(args.db)
    try:
        hits = search(conn, args.query, limit=args.limit, include=include,
                      channel=args.channel, user=args.user, after=after, before=before)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("(no matches)", file=sys.stderr)
        return 0
    for h in hits:
        body = _clean(h.get("text") if args.full else h.get("snippet"))
        print(f"{_fmt_ts(h.get('ts'))} | #{h.get('channel_name')} | {h.get('user_name')}: {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The single, locked build entry point — `python3 -m slack_log.pipeline`.

This is the choke point every build funnels through: the process-wide mutex
(see slack_log.lock) is acquired here, so a second concurrent build exits
immediately instead of corrupting the shared state. The Makefile's
personal-build / team-build targets are thin delegates to this. The split /
attach / index submodules can still be run standalone, but those are low-level
building blocks — a normal build always goes through this entry to be guarded.

Steps (mirroring the old Makefile chain):
  personal: fetch(slackdump) -> split -> attach(jsonl) -> index(jsonl)
  team:     fetch(slackdump) -> index(team, straight from sqlite) -> attach(team, from search.db)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from slack_log.lock import BuildLockHeld, build_lock
from slack_log.pipeline.attach import (
    download_attachments,
    iter_files_from_jsonl,
    iter_files_from_sqlite,
    load_token,
)
from slack_log.pipeline.index import build_index
from slack_log.pipeline.split import split

# Repo root (this file is <repo>/slack_log/pipeline/__main__.py). The build runs
# here so raw/ data/ search.db resolve relative to it, not the caller's cwd.
REPO = Path(__file__).resolve().parent.parent.parent


def fetch(raw: Path) -> None:
    """slackdump incremental pull (full archive on first run). slackdump is an
    external binary."""
    raw.mkdir(parents=True, exist_ok=True)
    if (raw / "slackdump.sqlite").exists():
        print("-> slackdump resume (incremental)")
        subprocess.run(
            ["slackdump", "resume", "-files=false", "-refresh", "."],
            cwd=raw, check=True)
    else:
        print("-> slackdump archive (first run, full)")
        subprocess.run(
            ["slackdump", "archive", "-o", ".", "-files=false"],
            cwd=raw, check=True)


def _attach(*, source_sqlite: Path | None, data_root: Path, max_mb: int) -> None:
    xoxc, xoxd = load_token()
    max_bytes = max_mb * 1024 * 1024
    if source_sqlite is not None:
        files = iter_files_from_sqlite(source_sqlite)
        src = f"{source_sqlite} (message_raw)"
    else:
        files = iter_files_from_jsonl(data_root)
        src = f"{data_root} (jsonl)"
    print(f"attach: source={src} -> {data_root} . cap={max_mb}MB")
    s = download_attachments(files, data_root, xoxc, xoxd, max_bytes)
    print(f"attach done downloaded={s['downloaded']} "
          f"meta_only={s['meta_only']} failed={s['failed']}")


def run(profile: str, max_mb: int, include: set[str] | None) -> None:
    raw, data, db = Path("raw"), Path("data"), Path("search.db")
    fetch(raw)
    if profile == "personal":
        conn = sqlite3.connect(raw / "slackdump.sqlite")
        print(f"split: {raw / 'slackdump.sqlite'} -> {data}")
        stats = split(conn, data)
        conn.close()
        print(f"split {sum(stats.values())} threads / {len(stats)} channels")
        _attach(source_sqlite=None, data_root=data, max_mb=max_mb)
        idx = build_index(data, db, include=include, profile="personal")
        print(f"[personal] indexed {idx['indexed']} messages -> {db}")
    else:  # team
        idx = build_index(raw / "slackdump.sqlite", db, include=include, profile="team")
        print(f"[team] indexed {idx['indexed']} messages -> {db}")
        _attach(source_sqlite=db, data_root=data, max_mb=max_mb)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m slack_log.pipeline",
        description="The single, locked slack-log build entry point.")
    ap.add_argument("--profile", choices=("personal", "team"), default="personal")
    ap.add_argument("--max-mb", type=int, default=10,
                    help="attachment size cap in MB (default 10)")
    ap.add_argument("--include", default="",
                    help="comma-separated subset of channel/dm/mpim (default: all)")
    args = ap.parse_args(argv)
    include = {p.strip() for p in args.include.split(",") if p.strip()} or None

    os.chdir(REPO)
    try:
        with build_lock():
            run(args.profile, args.max_mb, include)
    except BuildLockHeld as e:
        print(f"refused: {e}", file=sys.stderr)
        print("Another slack-log build is already running; exiting.", file=sys.stderr)
        print("After confirming no slackdump/python build process is alive "
              "(ps auxww | grep slackdump), retry; the lock auto-releases on "
              "process exit, no manual cleanup needed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

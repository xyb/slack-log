#!/usr/bin/env python3
"""
Download Slack attachments by a mime / size policy.

Two file sources, one per profile:
- personal — walk the data/ jsonl layer (iter_files_from_jsonl)
- team     — read search.db's message_raw table (iter_files_from_sqlite)

Both write into the same place: <data_root>/channels/<cid>/attachments/, with
a `<file_id>.meta.json` for every file (downloaded or not — the original Slack
URL is preserved so a skipped file can be fetched later).

Policy:
- images / text / code snippets / json / yaml / pdf, under the size cap → download
- zip / tar / gzip / video / audio → metadata only, always
- anything over the size cap (--max-mb, default 10) → metadata only

Auth: xoxc Slack browser token + xoxd cookie. Resolved in this order:
  1. environment variables SLACK_XOXC / SLACK_XOXD (both required as a pair)
  2. ./.env in the current working directory
  3. ~/.config/slack-log/.env (XDG-respecting)
  4. RuntimeError with instructions
"""

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from dotenv import dotenv_values
from tqdm import tqdm

# Per-user .env path. Override via monkeypatch in tests; users override via
# $XDG_CONFIG_HOME.
USER_DOTENV = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "slack-log" / ".env"

DEFAULT_MAX_MB = 10

# mimetypes worth keeping a local copy of (subject to the size cap).
DOWNLOADABLE_PREFIXES = (
    "image/", "text/", "application/json", "application/yaml",
    "application/pdf", "application/x-python",
)
# never download, whatever the size — archives and media balloon the volume.
NEVER_DOWNLOAD = ("application/zip", "application/x-tar", "application/x-gzip",
                  "video/", "audio/")


def should_download(mimetype: str, size: int, max_bytes: int) -> bool:
    """True when a file should be downloaded rather than left metadata-only.

    max_bytes is the configurable large-attachment cap (see --max-mb)."""
    if not mimetype:
        return False
    for prefix in NEVER_DOWNLOAD:
        if mimetype.startswith(prefix):
            return False
    if any(mimetype.startswith(p) for p in DOWNLOADABLE_PREFIXES):
        return size <= max_bytes
    return False


def load_token() -> tuple[str, str]:
    """Resolve (xoxc, xoxd) from env vars or .env files, else raise."""
    xoxc = os.environ.get("SLACK_XOXC")
    xoxd = os.environ.get("SLACK_XOXD")
    if xoxc and xoxd:
        return xoxc, xoxd
    # Try ./.env first (project-local), then user .env (XDG).
    for path in (Path(".env"), USER_DOTENV):
        if path.exists():
            values = dotenv_values(path)
            xoxc_f = values.get("SLACK_XOXC")
            xoxd_f = values.get("SLACK_XOXD")
            if xoxc_f and xoxd_f:
                return xoxc_f, xoxd_f
    raise RuntimeError(
        "missing Slack credentials. Either:\n"
        "  - export SLACK_XOXC and SLACK_XOXD env vars, or\n"
        "  - write them to ./.env (project-local) or\n"
        f"    {USER_DOTENV} (per-user).\n"
        "Both xoxc (browser Authorization header) and xoxd (cookie d) are needed."
    )


def download_file(url: str, dst: Path, xoxc: str, xoxd: str) -> bool:
    """下载 Slack 私有文件（需要 Bearer + cookie d）。"""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {xoxc}", "Cookie": f"d={xoxd}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(dst, "wb") as out:
            out.write(resp.read())
        return True
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"  ❌ {dst.name}: {e}")
        return False


# --- file sources ---------------------------------------------------------


def iter_files_from_jsonl(data_root: Path) -> Iterator[tuple[str, str, dict]]:
    """Personal profile — yield (channel_id, msg_ts, file_obj) from the jsonl layer."""
    channels_root = data_root / "channels"
    if not channels_root.exists():
        return
    for cdir in sorted(channels_root.iterdir()):
        threads = cdir / "threads"
        if not threads.is_dir():
            continue
        for jsonl in sorted(threads.glob("*.jsonl")):
            try:
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for f in msg.get("files") or []:
                    yield cdir.name, msg.get("ts"), f


def iter_files_from_sqlite(search_db: Path) -> Iterator[tuple[str, str, dict]]:
    """Team profile — yield (channel_id, msg_ts, file_obj) from search.db's
    message_raw table, so attachments work with no jsonl layer."""
    conn = sqlite3.connect(search_db)
    try:
        for cid, ts, data in conn.execute(
            "SELECT channel_id, ts, data FROM message_raw"
        ):
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            for f in msg.get("files") or []:
                yield cid, ts, f
    finally:
        conn.close()


# --- download -------------------------------------------------------------


def download_attachments(file_iter: Iterator[tuple[str, str, dict]], data_root: Path,
                         xoxc: str, xoxd: str, max_bytes: int) -> dict:
    """Download every file from file_iter into data_root/channels/<cid>/attachments/.

    Writes <file_id>.meta.json for every file. A single failure (bad URL, SSL,
    disk error) is recorded and the walk continues."""
    stats = {"downloaded": 0, "meta_only": 0, "failed": 0}
    for cid, msg_ts, file_obj in tqdm(file_iter, desc="attach", unit="file"):
        fid = file_obj.get("id")
        if not fid:
            continue
        att_dir = data_root / "channels" / cid / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        mimetype = file_obj.get("mimetype", "")
        size = file_obj.get("size", 0)

        meta_path = att_dir / f"{fid}.meta.json"
        meta = {
            "id": fid,
            "name": file_obj.get("name"),
            "mimetype": mimetype,
            "size": size,
            "filetype": file_obj.get("filetype"),
            "url_private_download": file_obj.get("url_private_download"),
            "url_private": file_obj.get("url_private"),
            "permalink": file_obj.get("permalink"),
            "created": file_obj.get("created"),
            "user": file_obj.get("user"),
            "_source_msg_ts": msg_ts,
            "_source_channel": cid,
            "_downloaded": False,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        if not should_download(mimetype, size, max_bytes):
            stats["meta_only"] += 1
            continue

        ft = file_obj.get("filetype") or "bin"
        dst = att_dir / f"{fid}.{ft}"
        if dst.exists():
            meta["_downloaded"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            stats["downloaded"] += 1
            continue

        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            stats["meta_only"] += 1
            continue

        # Any unexpected exception (SSL, disk full, ConnectionReset, …) must not
        # abort the walk — record it and move on.
        try:
            ok = download_file(url, dst, xoxc, xoxd)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fid}: unexpected {type(e).__name__}: {e}")
            ok = False
        if ok:
            meta["_downloaded"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            stats["downloaded"] += 1
        else:
            dst.unlink(missing_ok=True)
            stats["failed"] += 1

    return stats


def main():
    ap = argparse.ArgumentParser(description="Download Slack attachments by mime/size policy")
    ap.add_argument("data_root", type=Path, default=Path("./data"), nargs="?",
                    help="where channels/<cid>/attachments/ are written")
    ap.add_argument("--sqlite", type=Path, default=None,
                    help="team profile: read the file list from this search.db's "
                         "message_raw table instead of the jsonl layer")
    ap.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB,
                    help=f"skip an attachment larger than this many MB "
                         f"(default {DEFAULT_MAX_MB})")
    args = ap.parse_args()

    xoxc, xoxd = load_token()
    max_bytes = args.max_mb * 1024 * 1024
    if args.sqlite:
        files = iter_files_from_sqlite(args.sqlite)
        source = f"{args.sqlite} (message_raw)"
    else:
        files = iter_files_from_jsonl(args.data_root)
        source = f"{args.data_root} (jsonl)"
    print(f"attach: source={source} → {args.data_root} · cap={args.max_mb}MB")
    stats = download_attachments(files, args.data_root, xoxc, xoxd, max_bytes)
    print(f"✅ done — downloaded={stats['downloaded']} "
          f"meta_only={stats['meta_only']} failed={stats['failed']}")


if __name__ == "__main__":
    main()

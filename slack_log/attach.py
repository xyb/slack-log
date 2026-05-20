#!/usr/bin/env python3
"""
扫所有 thread jsonl 里的 files 字段，按阈值差异化下载附件。

策略：
- 图片 / 文本 / 代码片段 / PDF (<10MB)：下载真实文件 + .meta.json
- zip / 视频 / 大文件：只存 .meta.json（保留 url_private_download 备用）

认证：复用 xoxc/xoxd token from ~/.cursor/mcp.json。
"""

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from tqdm import tqdm

# (mimetype 前缀, 最大字节)
DOWNLOAD_RULES = [
    ("image/", 10 * 1024 * 1024),       # 图片 <10MB 下载
    ("text/", 5 * 1024 * 1024),         # 文本 <5MB
    ("application/json", 5 * 1024 * 1024),
    ("application/yaml", 5 * 1024 * 1024),
    ("application/pdf", 20 * 1024 * 1024),
    ("application/x-python", 5 * 1024 * 1024),
]
# 永不下载（即使小）
NEVER_DOWNLOAD = ["application/zip", "application/x-tar", "application/x-gzip", "video/", "audio/"]


def should_download(mimetype: str, size: int) -> bool:
    if not mimetype:
        return False
    for prefix in NEVER_DOWNLOAD:
        if mimetype.startswith(prefix):
            return False
    for prefix, max_size in DOWNLOAD_RULES:
        if mimetype.startswith(prefix):
            return size <= max_size
    return False  # 默认不下载


def load_token() -> tuple[str, str]:
    with open(os.path.expanduser("~/.cursor/mcp.json")) as f:
        d = json.load(f)
    env = d["mcpServers"]["slack"]["env"]
    return env["SLACK_MCP_XOXC_TOKEN"], env["SLACK_MCP_XOXD_TOKEN"]


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


def process_channel(channel_dir: Path, xoxc: str, xoxd: str) -> dict:
    """遍历 channel/threads/*.jsonl 的 files，按阈值下载或只存 meta。"""
    att_dir = channel_dir / "attachments"
    att_dir.mkdir(exist_ok=True)

    stats = {"meta_only": 0, "downloaded": 0, "failed": 0}

    for jsonl in (channel_dir / "threads").glob("*.jsonl"):
        with open(jsonl) as f:
            for line in f:
                msg = json.loads(line)
                for file_obj in msg.get("files") or []:
                    fid = file_obj.get("id")
                    if not fid:
                        continue
                    mimetype = file_obj.get("mimetype", "")
                    size = file_obj.get("size", 0)

                    # meta.json 永远生成
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
                        "_source_msg_ts": msg.get("ts"),
                        "_source_channel": channel_dir.name,
                    }
                    download_flag = should_download(mimetype, size)
                    meta["_downloaded"] = False
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

                    if not download_flag:
                        stats["meta_only"] += 1
                        continue

                    # 用文件原扩展名（filetype）拼下载文件路径
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

                    # Wrap the download call: any unexpected exception (SSL,
                    # disk full, ConnectionReset, etc. that download_file's
                    # inner try/except doesn't cover) must not abort the loop.
                    try:
                        ok = download_file(url, dst, xoxc, xoxd)
                    except Exception as e:
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
    ap = argparse.ArgumentParser()
    ap.add_argument("data_root", type=Path, default=Path("./data"), nargs="?")
    args = ap.parse_args()

    xoxc, xoxd = load_token()

    channels_root = args.data_root / "channels"
    channel_dirs = [c for c in channels_root.iterdir() if c.is_dir()]
    totals = {"downloaded": 0, "meta_only": 0, "failed": 0}
    for cdir in tqdm(channel_dirs, desc="channels", unit="ch"):
        stats = process_channel(cdir, xoxc, xoxd)
        for k in totals:
            totals[k] += stats[k]
    print(f"✅ done — downloaded={totals['downloaded']} meta_only={totals['meta_only']} failed={totals['failed']}")


if __name__ == "__main__":
    main()

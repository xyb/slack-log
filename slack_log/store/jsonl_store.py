"""JsonlStore — the personal profile's ArchiveStore.

Reads the data/ jsonl layer the splitter produces: per-thread jsonl,
per-channel index.jsonl, users.json, channels.json. Page data comes from those
files; full-text search comes from search.db (handled by the base class).
"""

import json
from pathlib import Path

from slack_log import render
from slack_log.store.base import ArchiveStore


class JsonlStore(ArchiveStore):
    """Personal profile — the data/ jsonl directory is the source of truth."""

    def __init__(self, data_root: Path, db_path: Path):
        self.data_root = Path(data_root)
        self.search_db = Path(db_path)
        self._users: dict | None = None
        self._channels: dict | None = None

    def users(self) -> dict:
        if self._users is None:
            p = self.data_root / "users.json"
            self._users = json.loads(p.read_text()) if p.exists() else {}
        return self._users

    def channels(self) -> dict:
        if self._channels is None:
            p = self.data_root / "channels.json"
            self._channels = json.loads(p.read_text()) if p.exists() else {}
        return self._channels

    def list_channels(self) -> list[str]:
        croot = self.data_root / "channels"
        if not croot.exists():
            return []
        return sorted(c.name for c in croot.iterdir() if c.is_dir())

    def thread_meta(self, cid: str) -> list[dict]:
        return render.load_thread_meta(self.data_root / "channels" / cid)

    def load_thread(self, cid: str, ts: str) -> list[dict] | None:
        ttp = self.data_root / "channels" / cid / "threads" / f"{ts}.jsonl"
        if not ttp.exists():
            return None
        return render.load_thread(ttp)

    def global_groups(self, include: set[str] | None = None) -> dict:
        return render.build_global_groups(
            self.data_root, self.channels(), self.users(), include=include
        )

    def attachments_dir(self, cid: str) -> Path:
        return self.data_root / "channels" / cid / "attachments"

    def fetched_at(self) -> str:
        for cand in (Path("raw/slackdump.sqlite"), self.data_root / "users.json"):
            try:
                if cand.exists():
                    return str(cand.stat().st_mtime)
            except OSError:
                pass
        return ""

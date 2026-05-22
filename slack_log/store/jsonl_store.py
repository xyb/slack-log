"""JsonlStore — the personal profile's ArchiveStore.

Reads the data/ jsonl layer the splitter produces: per-thread jsonl,
per-channel index.jsonl, users.json, channels.json. Page data comes from those
files; full-text search comes from search.db (handled by the base class).
"""

import json
from pathlib import Path

from slack_log.store.base import ArchiveStore, assemble_global_groups


class JsonlStore(ArchiveStore):
    """Personal profile — the data/ jsonl directory is the source of truth.

    db_path is optional: the static-HTML exporter builds a JsonlStore purely to
    read pages and never touches search.db.
    """

    def __init__(self, data_root: Path, db_path: Path | None = None):
        self.data_root = Path(data_root)
        self.search_db = Path(db_path) if db_path else None
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
        index_path = self.data_root / "channels" / cid / "index.jsonl"
        out: list[dict] = []
        if index_path.exists():
            with open(index_path) as f:
                for line in f:
                    if line.strip():
                        out.append(json.loads(line))
        return out

    def load_thread(self, cid: str, ts: str) -> list[dict] | None:
        ttp = self.data_root / "channels" / cid / "threads" / f"{ts}.jsonl"
        if not ttp.exists():
            return None
        msgs: list[dict] = []
        with open(ttp) as f:
            for line in f:
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
        return msgs

    def global_groups(self, include: set[str] | None = None) -> dict:
        entries: list[tuple] = []
        croot = self.data_root / "channels"
        if croot.exists():
            for cdir in croot.iterdir():
                if not cdir.is_dir():
                    continue
                threads = cdir / "threads"
                n_threads = len(list(threads.glob("*.jsonl"))) if threads.exists() else 0
                entries.append((cdir.name, self.channels().get(cdir.name), n_threads))
        return assemble_global_groups(entries, self.users(), include)

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

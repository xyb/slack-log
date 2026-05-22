"""SqliteStore — the team profile's ArchiveStore.

Reads every page from the extended search.db the indexer's team ETL builds:
the message_raw / threads / channels / users tables. No jsonl, no data/
directory — the team server is one process reading one SQLite file.

Its page-data output matches JsonlStore's field-for-field; the parametrized
contract test (tests/test_store_contract.py) enforces that.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from slack_log.store.base import ArchiveStore, assemble_global_groups


class SqliteStore(ArchiveStore):
    """Team profile — the extended search.db is the single source of truth."""

    def __init__(self, db_path: Path, data_root: Path | None = None):
        self.search_db = Path(db_path)
        # attach.py downloads team attachments into data_root/channels/<cid>/
        # attachments/ — the same layout the personal profile uses. Default
        # beside the db for the standard <root>/{search.db, data/} layout.
        self._data_root = (
            Path(data_root) if data_root else self.search_db.parent / "data"
        )
        self._users: dict | None = None
        self._channels: dict | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.search_db)
        conn.row_factory = sqlite3.Row
        return conn

    def users(self) -> dict:
        if self._users is None:
            with closing(self._connect()) as conn:
                self._users = {
                    r["id"]: json.loads(r["profile"])
                    for r in conn.execute("SELECT id, profile FROM users")
                }
        return self._users

    def channels(self) -> dict:
        if self._channels is None:
            with closing(self._connect()) as conn:
                self._channels = {
                    r["id"]: {
                        "name": r["name"],
                        "kind": r["kind"],
                        "is_im": bool(r["is_im"]),
                        "is_mpim": bool(r["is_mpim"]),
                        "other_uid": r["other_uid"],
                        "members": json.loads(r["members"] or "[]"),
                    }
                    for r in conn.execute(
                        "SELECT id, name, kind, is_im, is_mpim, other_uid, members FROM channels"
                    )
                }
        return self._channels

    def list_channels(self) -> list[str]:
        with closing(self._connect()) as conn:
            return [r["id"] for r in conn.execute("SELECT id FROM channels")]

    def thread_meta(self, cid: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE channel_id = ? ORDER BY thread_ts", (cid,)
            ).fetchall()
        # Reconstruct the splitter's index.jsonl row shape exactly: SQLite has
        # no bool / list, so is_thread / has_files / participants are restored.
        return [
            {
                "thread_ts": r["thread_ts"],
                "first_ts": r["first_ts"],
                "first_user": r["first_user"],
                "first_author_display": r["first_author_display"],
                "first_author_avatar": r["first_author_avatar"],
                "first_text_preview": r["first_text_preview"],
                "latest_reply_ts": r["latest_reply_ts"],
                "reply_count": r["reply_count"],
                "msg_count": r["msg_count"],
                "is_thread": bool(r["is_thread"]),
                "has_files": bool(r["has_files"]),
                "participants": json.loads(r["participants"] or "[]"),
            }
            for r in rows
        ]

    def load_thread(self, cid: str, ts: str) -> list[dict] | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT data FROM message_raw "
                "WHERE channel_id = ? AND thread_ts = ? ORDER BY ts",
                (cid, ts),
            ).fetchall()
        if not rows:
            return None
        return [json.loads(r["data"]) for r in rows]

    def global_groups(self, include: set[str] | None = None) -> dict:
        with closing(self._connect()) as conn:
            entries = [
                (
                    r["id"],
                    {"name": r["name"], "is_im": bool(r["is_im"]),
                     "is_mpim": bool(r["is_mpim"]), "other_uid": r["other_uid"],
                     "members": json.loads(r["members"] or "[]")},
                    r["thread_count"],
                )
                for r in conn.execute(
                    "SELECT id, name, is_im, is_mpim, other_uid, members, thread_count "
                    "FROM channels"
                )
            ]
        return assemble_global_groups(entries, self.users(), include)

    def attachments_dir(self, cid: str) -> Path:
        return self._data_root / "channels" / cid / "attachments"

    def fetched_at(self) -> str:
        try:
            return str(self.search_db.stat().st_mtime)
        except OSError:
            return ""

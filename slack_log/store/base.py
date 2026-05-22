"""ArchiveStore — the storage abstraction the web layer depends on.

The FastAPI app never touches a jsonl file or a SQLite table directly; it asks
an ArchiveStore. Two implementations back the two product profiles:

  JsonlStore  — personal: reads the data/ jsonl layer (splitter output)
  SqliteStore — team: reads the extended search.db

Page-data methods (thread_meta / load_thread / global_groups / ...) are
abstract — each profile reads its own source. The full-text search index
(search.db's `messages` FTS5 table) is profile-independent, so search() and
user_messages() are concrete here, shared by both stores; a subclass only has
to set self.search_db.
"""

import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from pathlib import Path

from slack_log import indexer


class ArchiveStore(ABC):
    """Read-only access to one Slack archive, abstracted over its storage."""

    # Subclasses set this to the search.db path before search() / user_messages()
    # are called.
    search_db: Path

    # --- page data — profile-specific ---

    @abstractmethod
    def users(self) -> dict:
        """{uid: profile} for the whole archive."""

    @abstractmethod
    def channels(self) -> dict:
        """{cid: meta} for the whole archive (name, is_im, is_mpim, ...)."""

    @abstractmethod
    def list_channels(self) -> list[str]:
        """Every channel id present in the archive."""

    @abstractmethod
    def thread_meta(self, cid: str) -> list[dict]:
        """Per-thread metadata rows for one channel, un-enriched.

        Empty list for an unknown channel. Each row follows the index.jsonl
        schema: thread_ts, first_ts, first_user, first_text_preview,
        latest_reply_ts, reply_count, msg_count, is_thread, has_files,
        participants, first_author_display, first_author_avatar.
        """

    @abstractmethod
    def load_thread(self, cid: str, ts: str) -> list[dict] | None:
        """Raw Slack message dicts for one thread, ts-ascending.

        None when the thread does not exist.
        """

    @abstractmethod
    def global_groups(self, include: set[str] | None = None) -> dict:
        """{channels, dms, mpims} groups for the home page.

        include: subset of {channel, dm, mpim} to keep. None = all.
        """

    @abstractmethod
    def attachments_dir(self, cid: str) -> Path:
        """Directory holding channel cid's downloaded attachments.

        May not exist — callers treat a missing file as 'not downloaded'.
        """

    @abstractmethod
    def fetched_at(self) -> str:
        """Archive freshness as a unix-epoch string (browser renders it local)."""

    # --- search index — shared, profile-independent ---

    def search(self, q: str, limit: int = 50,
               include: set[str] | None = None) -> list[dict]:
        """FTS5 full-text search over search.db."""
        with closing(sqlite3.connect(self.search_db)) as conn:
            return indexer.search(conn, q, limit=limit, include=include)

    def user_messages(self, uid: str, limit: int = 500,
                      include: set[str] | None = None) -> list[dict]:
        """Every message by uid, newest first.

        Each row: {ts, thread_ts, channel_id, channel_name, user_name, text}.
        include filters by channel kind (subset of {channel, dm, mpim}).
        """
        kind_clause, kind_params = "", []
        if include:
            ph = ",".join("?" * len(include))
            kind_clause = f" AND kind IN ({ph})"
            kind_params = sorted(include)
        with closing(sqlite3.connect(self.search_db)) as conn:
            cur = conn.execute(
                f"SELECT ts, thread_ts, channel_id, channel_name, user_name, text "
                f"FROM messages WHERE user_id = ?{kind_clause} "
                f"ORDER BY ts DESC LIMIT ?",
                (uid, *kind_params, limit),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["text"] = indexer.join_cjk(r["text"])
        return rows

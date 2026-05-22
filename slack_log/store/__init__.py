"""Storage layer — the ArchiveStore abstraction + per-profile implementations.

The web layer depends only on ArchiveStore, never on a jsonl path or a SQLite
table. JsonlStore backs the personal profile, SqliteStore the team profile.
"""

from slack_log.store.base import ArchiveStore
from slack_log.store.jsonl_store import JsonlStore

__all__ = ["ArchiveStore", "JsonlStore"]

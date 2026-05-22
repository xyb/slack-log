"""Profile + Config — the one switch that splits slack-log into two products.

slack-log ships as a single codebase with two product shapes:

  personal — runs on your laptop. The splitter writes the data/ jsonl layer;
    that jsonl is the core deliverable (grep it, feed it to an AI, browse it
    locally). The server reads jsonl, static HTML export is available.

  team — runs on a server. No jsonl: the indexer ETLs slackdump.sqlite
    straight into search.db and the server reads search.db only. FastAPI +
    OIDC SSO + the in-process refresh scheduler + Kubernetes.

`SLACK_LOG_PROFILE` is the switch. Everything else here just records the env
surface so the web factory and the refresh script agree on it.
"""

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Profile(str, Enum):
    """Which product shape this process is running as."""

    PERSONAL = "personal"
    TEAM = "team"


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration, loaded once from the environment."""

    profile: Profile
    root: Path
    data_root: Path        # personal: the jsonl layer; both: where attachments land
    db_path: Path          # search.db — both profiles
    sqlite_path: Path      # raw/slackdump.sqlite — slackdump's archive
    include: set[str] | None
    emit_jsonl: bool       # team escape hatch: also write the jsonl layer
    download_attachments: bool   # whether the refresh downloads attachments
    attachment_max_mb: int       # skip an attachment larger than this (MB)
    sync_token: str | None
    sync_interval: float

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        """Build a Config from environment variables (os.environ by default).

        An unknown SLACK_LOG_PROFILE raises ValueError — fail fast rather than
        silently fall back to the wrong product shape.
        """
        env = os.environ if env is None else env
        root = Path(env.get("SLACK_LOG_ROOT", "."))
        profile = Profile(env.get("SLACK_LOG_PROFILE", "personal"))
        include = {p.strip() for p in env.get("SLACK_LOG_INCLUDE", "").split(",") if p.strip()}
        return cls(
            profile=profile,
            root=root,
            data_root=Path(env.get("SLACK_LOG_DATA", root / "data")),
            db_path=Path(env.get("SLACK_LOG_DB", root / "search.db")),
            sqlite_path=Path(env.get("SLACK_LOG_SQLITE", root / "raw" / "slackdump.sqlite")),
            include=include or None,
            emit_jsonl=_truthy(env.get("SLACK_LOG_EMIT_JSONL", "")),
            download_attachments=_truthy(env.get("SLACK_LOG_ATTACHMENTS", "1")),
            attachment_max_mb=int(env.get("SLACK_LOG_ATTACHMENT_MAX_MB", "10") or "10"),
            sync_token=env.get("SLACK_LOG_SYNC_TOKEN") or None,
            sync_interval=float(env.get("SLACK_LOG_SYNC_INTERVAL", "0") or "0"),
        )

    @property
    def is_team(self) -> bool:
        return self.profile is Profile.TEAM

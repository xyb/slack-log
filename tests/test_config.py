"""Tests for slack_log.config — Profile + Config.from_env."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slack_log.pipeline import index
from slack_log.web.app import create_app_from_env
from slack_log.config import Config, Profile


# --- Config.from_env ------------------------------------------------------

def test_defaults_to_personal():
    cfg = Config.from_env({})
    assert cfg.profile is Profile.PERSONAL
    assert cfg.is_team is False
    assert cfg.include is None
    assert cfg.emit_jsonl is False
    assert cfg.sync_interval == 0.0
    assert cfg.sync_token is None


def test_team_profile():
    cfg = Config.from_env({"SLACK_LOG_PROFILE": "team"})
    assert cfg.profile is Profile.TEAM
    assert cfg.is_team is True


def test_unknown_profile_fails_fast():
    with pytest.raises(ValueError):
        Config.from_env({"SLACK_LOG_PROFILE": "enterprise"})


def test_paths_derive_from_root():
    cfg = Config.from_env({"SLACK_LOG_ROOT": "/srv/slack-log"})
    assert cfg.data_root == Path("/srv/slack-log/data")
    assert cfg.db_path == Path("/srv/slack-log/search.db")
    assert cfg.sqlite_path == Path("/srv/slack-log/raw/slackdump.sqlite")


def test_explicit_path_overrides_root():
    cfg = Config.from_env({"SLACK_LOG_ROOT": "/srv", "SLACK_LOG_DB": "/other/s.db"})
    assert cfg.db_path == Path("/other/s.db")


def test_include_parsed_into_set():
    assert Config.from_env({"SLACK_LOG_INCLUDE": "channel, dm"}).include == {"channel", "dm"}
    assert Config.from_env({"SLACK_LOG_INCLUDE": ""}).include is None


def test_emit_jsonl_truthiness():
    assert Config.from_env({"SLACK_LOG_EMIT_JSONL": "1"}).emit_jsonl is True
    assert Config.from_env({"SLACK_LOG_EMIT_JSONL": "true"}).emit_jsonl is True
    assert Config.from_env({"SLACK_LOG_EMIT_JSONL": "0"}).emit_jsonl is False


def test_sync_settings():
    cfg = Config.from_env({"SLACK_LOG_SYNC_TOKEN": "tok", "SLACK_LOG_SYNC_INTERVAL": "3600"})
    assert cfg.sync_token == "tok"
    assert cfg.sync_interval == 3600.0


# --- create_app_from_env wires the profile through ------------------------

def test_create_app_from_env_team_serves_from_sqlite(
    sqlite_with_threads: Path, tmp_path: Path, monkeypatch
):
    """SLACK_LOG_PROFILE=team → create_app_from_env builds a SqliteStore-backed
    app that serves pages straight from search.db."""
    db = tmp_path / "search.db"
    index.build_index(sqlite_with_threads, db, profile="team")
    for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SLACK_LOG_PROFILE", "team")
    monkeypatch.setenv("SLACK_LOG_DB", str(db))

    client = TestClient(create_app_from_env())
    assert client.get("/").status_code == 200
    assert client.get("/channels/C001").status_code == 200
    thread = client.get("/channels/C001/threads/1700000100.000002")
    assert thread.status_code == 200
    assert "parent thread" in thread.text


def test_create_app_from_env_personal_serves_from_jsonl(
    sqlite_with_threads: Path, tmp_path: Path, monkeypatch
):
    """Default profile → JsonlStore-backed app reading the data/ jsonl layer."""
    from slack_log.pipeline.split import split

    data = tmp_path / "data"
    conn = sqlite3.connect(sqlite_with_threads)
    split(conn, data)
    conn.close()
    db = tmp_path / "search.db"
    index.build_index(data, db, profile="personal")
    for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("SLACK_LOG_PROFILE", raising=False)
    monkeypatch.setenv("SLACK_LOG_DB", str(db))
    monkeypatch.setenv("SLACK_LOG_DATA", str(data))

    client = TestClient(create_app_from_env())
    assert client.get("/").status_code == 200
    assert client.get("/channels/C001").status_code == 200

"""Sync manager mutex + the /sync API."""

import asyncio

from fastapi.testclient import TestClient

from slack_log import indexer, server
from slack_log.sync import SyncManager


def _script(tmp_path, body):
    s = tmp_path / "fake-refresh.sh"
    s.write_text(f"#!/bin/sh\n{body}\n")
    return s


# ---- SyncManager ----------------------------------------------------------

def test_mutex_rejects_second_trigger(tmp_path):
    """A second trigger while one is running returns False, and does not
    overwrite the running sync's metadata."""
    mgr = SyncManager(script=_script(tmp_path, "sleep 0.3"), cwd=tmp_path)

    async def scenario():
        first = await mgr.trigger("first")
        second = await mgr.trigger("second")  # first still running
        await mgr._task
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert mgr.running is False
    assert mgr.last_result == "success"
    assert mgr.last_trigger == "first"  # rejected trigger didn't clobber it


def test_records_failure_on_nonzero_exit(tmp_path):
    mgr = SyncManager(script=_script(tmp_path, "exit 1"), cwd=tmp_path)

    async def scenario():
        await mgr.trigger("t")
        await mgr._task

    asyncio.run(scenario())
    assert mgr.last_result == "failed"


def test_retrigger_after_finish(tmp_path):
    """Once a run finishes the manager is free to trigger again."""
    mgr = SyncManager(script=_script(tmp_path, "true"), cwd=tmp_path)

    async def scenario():
        await mgr.trigger("a")
        await mgr._task
        again = await mgr.trigger("b")
        await mgr._task
        return again

    again = asyncio.run(scenario())
    assert again is True
    assert mgr.last_trigger == "b"


# ---- /sync API ------------------------------------------------------------

def _app(tmp_path, sync_token="tok", body="true"):
    data = tmp_path / "data"
    (data / "channels").mkdir(parents=True)
    (data / "users.json").write_text("{}")
    (data / "channels.json").write_text("{}")
    db = tmp_path / "search.db"
    indexer.build_index(data, db)
    html = tmp_path / "html"
    html.mkdir()
    return server.create_app(
        db_path=db, html_root=html, data_root=data,
        sync_token=sync_token, sync_script=_script(tmp_path, body),
    )


def test_sync_api_requires_token(tmp_path):
    client = TestClient(_app(tmp_path), raise_server_exceptions=False)
    assert client.post("/sync").status_code == 401
    assert client.post("/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_sync_api_disabled_without_token(tmp_path):
    """No token configured → the API refuses outright (503), never wide open."""
    client = TestClient(_app(tmp_path, sync_token=None), raise_server_exceptions=False)
    assert client.post("/sync").status_code == 503
    assert client.get("/sync").status_code == 503


def test_sync_api_triggers(tmp_path):
    """A valid token starts a sync (202) and GET /sync reports state.

    The 409 'already running' path is covered by the SyncManager mutex
    tests above — asserting it through TestClient is flaky because a
    background task's lifetime across requests is not guaranteed there
    (it is fine under a real uvicorn event loop)."""
    client = TestClient(_app(tmp_path, body="true"), raise_server_exceptions=False)
    hdr = {"Authorization": "Bearer tok"}
    assert client.post("/sync", headers=hdr).status_code == 202
    body = client.get("/sync", headers=hdr).json()
    assert "running" in body
    assert body["last_trigger"] == "api"

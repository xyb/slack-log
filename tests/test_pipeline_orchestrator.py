"""Tests for slack_log.pipeline.__main__ — the single, locked build entry.

These exercise orchestration logic, not real slackdump/indexing: the step
order, the profile branch, and that a held lock makes a second build exit
cleanly (return 1 + run is never called).
"""

import contextlib

import pytest

from slack_log.lock import BuildLockHeld
from slack_log.pipeline import __main__ as orch


class _DummyConn:
    def close(self):
        pass


@pytest.fixture
def record_steps(monkeypatch):
    """Replace each step with a stub that just records call order; nothing real
    runs. Returns the order list."""
    calls = []
    monkeypatch.setattr(orch, "fetch", lambda raw: calls.append("fetch"))
    monkeypatch.setattr(orch, "split",
                        lambda conn, out: (calls.append("split"), {})[1])
    monkeypatch.setattr(orch.sqlite3, "connect", lambda p: _DummyConn())
    monkeypatch.setattr(orch, "_attach",
                        lambda **kw: calls.append(
                            f"attach:{'sqlite' if kw.get('source_sqlite') else 'jsonl'}"))
    monkeypatch.setattr(orch, "build_index",
                        lambda src, db, **kw: (calls.append(f"index:{kw.get('profile')}"),
                                               {"indexed": 0})[1])
    return calls


# --- run(): step order + profile branch -----------------------------------

def test_personal_runs_fetch_split_attach_index_in_order(record_steps):
    orch.run("personal", max_mb=10, include=None)
    assert record_steps == ["fetch", "split", "attach:jsonl", "index:personal"]


def test_team_runs_fetch_index_attach_in_order(record_steps):
    orch.run("team", max_mb=10, include=None)
    # team: index straight from sqlite, then attach from search.db; no split.
    assert record_steps == ["fetch", "index:team", "attach:sqlite"]


def test_personal_does_not_run_team_steps(record_steps):
    orch.run("personal", max_mb=10, include=None)
    assert "index:team" not in record_steps
    assert "attach:sqlite" not in record_steps


# --- main(): lock semantics -----------------------------------------------

def test_main_runs_under_lock_and_returns_0(monkeypatch):
    seen = {}
    monkeypatch.setattr(orch.os, "chdir", lambda p: None)
    monkeypatch.setattr(orch, "build_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(orch, "run",
                        lambda profile, max_mb, include: seen.update(
                            profile=profile, max_mb=max_mb, include=include))
    rc = orch.main(["--profile", "personal", "--max-mb", "7"])
    assert rc == 0
    assert seen == {"profile": "personal", "max_mb": 7, "include": None}


def test_main_exits_1_when_lock_held(monkeypatch, capsys):
    def _held(*a, **k):
        raise BuildLockHeld(orch.Path("x.lock"))
    ran = []
    monkeypatch.setattr(orch.os, "chdir", lambda p: None)
    monkeypatch.setattr(orch, "build_lock", _held)
    monkeypatch.setattr(orch, "run", lambda *a, **k: ran.append(1))
    rc = orch.main(["--profile", "personal"])
    assert rc == 1
    assert ran == []                       # lock held -> run must not execute
    err = capsys.readouterr().err
    assert "already running" in err        # clear refusal message


def test_main_parses_include_into_set(monkeypatch):
    seen = {}
    monkeypatch.setattr(orch.os, "chdir", lambda p: None)
    monkeypatch.setattr(orch, "build_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(orch, "run",
                        lambda profile, max_mb, include: seen.update(include=include))
    orch.main(["--include", "channel, dm ,mpim"])
    assert seen["include"] == {"channel", "dm", "mpim"}

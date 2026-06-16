"""Build mutex — the single choke point that serializes slack-log builds.

Why it lives here: two builds running `slackdump resume` + index at the same
time corrupt raw/slackdump.sqlite and search.db. The lock must sit at the core
entry that every path funnels through (this module, used by the one build
entry point `slack_log.pipeline.__main__`), not in the Makefile — the Makefile
is just one caller, and running a submodule directly would bypass it.

Mechanism: fcntl.flock(LOCK_EX | LOCK_NB), advisory and non-blocking — a second
build that can't get the lock raises BuildLockHeld and exits immediately. The
lock is tied to the open file descriptor, so the kernel releases it when the
process exits (normally, on crash, or on SIGKILL) — no stale lock left behind
(unlike a mkdir lockfile, which needs manual cleanup after a crash).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

# Anchor the lockfile to the repo root (this file is <repo>/slack_log/lock.py)
# so it doesn't drift with the caller's cwd.
LOCK_PATH = Path(__file__).resolve().parent.parent / ".slacklog-build.lock"


class BuildLockHeld(Exception):
    """Another build already holds the lock; this one can't acquire it."""

    def __init__(self, path: Path):
        super().__init__(f"another slack-log build holds the lock: {path}")
        self.path = path


@contextlib.contextmanager
def build_lock(lock_path: Path = LOCK_PATH):
    """Process-wide mutex: only one build at a time. Non-blocking — raises
    BuildLockHeld instead of waiting if the lock is already held."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    # Acquire phase: if the lock is taken, close the fd and raise — do not fall
    # through to the finally below (that would double-close the fd).
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise BuildLockHeld(lock_path) from e
    # Held: a dedicated try/finally guarantees a single unlock + close.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())  # record pid for debugging
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

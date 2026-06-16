"""Tests for slack_log.lock — the build mutex (fcntl.flock).

The lock is the core of build concurrency safety, so its logic must be covered:
acquire, reject when held, release on exit, and auto-release when the holder
process dies.
"""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from slack_log.lock import BuildLockHeld, build_lock

REPO = Path(__file__).resolve().parent.parent


def test_acquire_and_release(tmp_path):
    lp = tmp_path / "b.lock"
    with build_lock(lp):
        pass
    # Re-acquirable after release (no raise).
    with build_lock(lp):
        pass


def test_second_acquire_is_rejected(tmp_path):
    lp = tmp_path / "b.lock"
    with build_lock(lp):
        with pytest.raises(BuildLockHeld):
            with build_lock(lp):
                pass


def test_rejection_is_nonblocking(tmp_path):
    """A held lock is rejected immediately, not waited on."""
    lp = tmp_path / "b.lock"
    with build_lock(lp):
        t0 = time.monotonic()
        with pytest.raises(BuildLockHeld):
            with build_lock(lp):
                pass
        assert time.monotonic() - t0 < 1.0


def test_buildlockheld_carries_path(tmp_path):
    lp = tmp_path / "b.lock"
    with build_lock(lp):
        try:
            with build_lock(lp):
                pass
            pytest.fail("should have raised")
        except BuildLockHeld as e:
            assert e.path == lp


def test_pid_written_to_lockfile(tmp_path):
    lp = tmp_path / "b.lock"
    with build_lock(lp):
        assert lp.read_text().strip() == str(os.getpid())


def test_released_when_holder_process_dies(tmp_path):
    """After the holder is SIGKILLed, the kernel must release the lock (no
    stale deadlock)."""
    lp = tmp_path / "b.lock"
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO)!r})
        from slack_log.lock import build_lock
        with build_lock({str(lp)!r}):
            print("HELD", flush=True)
            time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"  # holder has the lock
        with pytest.raises(BuildLockHeld):                # nobody else can take it now
            with build_lock(lp):
                pass
        proc.kill()                                       # hard-kill the holder
        proc.wait(timeout=5)
        # Kernel released it -> acquirable again.
        for _ in range(50):
            try:
                with build_lock(lp):
                    break
            except BuildLockHeld:
                time.sleep(0.1)
        else:
            pytest.fail("lock not released after holder was killed")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_exception_inside_lock_still_releases(tmp_path):
    lp = tmp_path / "b.lock"
    with pytest.raises(ValueError):
        with build_lock(lp):
            raise ValueError("boom")
    # Lock must be released after the exception.
    with build_lock(lp):
        pass

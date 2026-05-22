"""In-process sync manager — serialises slackdump refresh runs.

A single `SyncManager` owns the refresh pipeline (`scripts/refresh.sh`:
archive → split → attach → index). Both the background scheduler and the
`POST /sync` API go through it, so at most one refresh runs at a time.

The mutex is a plain in-process flag, not a file lock. That is reliable
*because* the server runs as a single uvicorn worker — and the RWO data
volume already forces the Deployment to a single replica, so there is
exactly one process that can ever start a sync. `trigger()` runs to its
first `await` without interruption (single event loop), so the
check-and-set on `_running` is atomic.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path


class SyncManager:
    """Owns the one-at-a-time refresh pipeline."""

    def __init__(self, script: Path, cwd: Path):
        self._script = Path(script)
        self._cwd = Path(cwd)
        self._running = False
        self._task: asyncio.Task | None = None
        self.last_started: float | None = None
        self.last_finished: float | None = None
        self.last_result: str = "never"  # never / success / failed
        self.last_trigger: str = ""
        self.last_log_tail: str = ""

    @property
    def running(self) -> bool:
        return self._running

    async def trigger(self, reason: str) -> bool:
        """Start a refresh unless one is already running.

        Returns True if a refresh was started, False if one was already in
        progress. The caller decides what a False means — the scheduler
        skips the tick, the API returns 409.
        """
        if self._running:
            return False
        self._running = True
        self.last_started = time.time()
        self.last_trigger = reason
        self._task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sh", str(self._script),
                cwd=str(self._cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # Stream the refresh output line by line — print each to the
            # server's own stdout so archive/split/attach/index progress is
            # visible live in the pod logs, not only in last_log_tail after
            # the whole run ends.
            tail: deque[str] = deque(maxlen=40)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                print(f"[sync] {line}", flush=True)
                tail.append(line)
            await proc.wait()
            self.last_log_tail = "\n".join(list(tail)[-20:])
            self.last_result = "success" if proc.returncode == 0 else "failed"
        except Exception as e:  # noqa: BLE001 — record any failure, never crash the loop
            self.last_result = "failed"
            self.last_log_tail = f"{type(e).__name__}: {e}"
        finally:
            self.last_finished = time.time()
            self._running = False

    def status(self) -> dict:
        """Machine-readable sync state for GET /sync."""
        return {
            "running": self._running,
            "last_trigger": self.last_trigger,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_result": self.last_result,
            "last_log_tail": self.last_log_tail,
        }


async def scheduler_loop(manager: SyncManager, interval: float) -> None:
    """Background loop: trigger a sync every `interval` seconds.

    A tick that lands while a sync is still running is simply skipped
    (`trigger` returns False) — no queueing, no overlap.
    """
    while True:
        await asyncio.sleep(interval)
        await manager.trigger("scheduled")

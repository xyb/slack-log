"""TDD round 1: splitter must survive a single corrupt thread."""

import sqlite3
from pathlib import Path

from slack_log import splitter


def test_invalid_json_in_one_thread_does_not_stop_others(sqlite_with_threads: Path, tmp_path: Path, capsys):
    """One thread has bytes that can't json.loads; the other 3 threads must still split.

    Before the fix: json.loads raises and the entire split_threads() aborts —
    valid threads in later channels (or even within the same channel) are never written.
    """
    out_root = tmp_path / "data"
    out_root.mkdir()

    conn = sqlite3.connect(sqlite_with_threads)
    stats = splitter.split_threads(conn, out_root)
    conn.close()

    # 3 channels of valid thread anchors should still produce jsonl files.
    # C001 has anchors at 1700000000.000001 and 1700000100.000002 -> 2 files
    # C002 has anchors at 1700000200.000004 (valid) and 1700000300.000005 (corrupt) -> 1 valid file
    c001_threads = list((out_root / "channels" / "C001" / "threads").glob("*.jsonl"))
    c002_threads = list((out_root / "channels" / "C002" / "threads").glob("*.jsonl"))

    assert len(c001_threads) == 2, f"C001 should have 2 thread files, got {[t.name for t in c001_threads]}"
    assert any("1700000200" in t.name for t in c002_threads), (
        f"C002's valid thread (1700000200) must still be written, got {[t.name for t in c002_threads]}"
    )
    # stats reflect that C001 produced its 2 anchors fully
    assert stats["C001"] == 2

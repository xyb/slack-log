"""Integration tests for attach — both file sources + the download loop."""

import json
import sqlite3
from pathlib import Path

from slack_log.pipeline import attach

_10MB = 10 * 1024 * 1024


def _img(fid: str) -> dict:
    return {"id": fid, "name": f"{fid}.png", "mimetype": "image/png", "size": 1000,
            "filetype": "png", "url_private_download": f"https://example.com/{fid}.png"}


def _zip(fid: str) -> dict:
    return {"id": fid, "name": f"{fid}.zip", "mimetype": "application/zip", "size": 1000,
            "filetype": "zip", "url_private_download": f"https://example.com/{fid}.zip"}


def _fake_download(url, dst, xoxc, xoxd):
    dst.write_bytes(b"fake")
    return True


# --- file sources ---------------------------------------------------------

def test_iter_files_from_jsonl(tmp_path: Path):
    threads = tmp_path / "channels" / "C001" / "threads"
    threads.mkdir(parents=True)
    (threads / "1.0.jsonl").write_text(
        json.dumps({"ts": "1.0", "files": [_img("F1"), _zip("F2")]}) + "\n")
    files = [(c, t, f["id"]) for c, t, f in attach.iter_files_from_jsonl(tmp_path)]
    assert files == [("C001", "1.0", "F1"), ("C001", "1.0", "F2")]


def test_iter_files_from_sqlite(tmp_path: Path):
    """The team profile reads the file list straight from search.db's message_raw."""
    db = tmp_path / "search.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE message_raw (channel_id TEXT, ts TEXT, thread_ts TEXT, data TEXT)")
    conn.execute("INSERT INTO message_raw VALUES (?, ?, ?, ?)",
                 ("C001", "2.0", "2.0", json.dumps({"ts": "2.0", "files": [_img("F9")]})))
    conn.commit()
    conn.close()
    files = [(c, t, f["id"]) for c, t, f in attach.iter_files_from_sqlite(db)]
    assert files == [("C001", "2.0", "F9")]


# --- download_attachments -------------------------------------------------

def test_download_attachments_happy_path(tmp_path: Path, monkeypatch):
    """One image downloads; one zip stays metadata-only."""
    monkeypatch.setattr(attach, "download_file", _fake_download)
    files = [("C001", "1.0", _img("F1")), ("C001", "1.0", _zip("F2"))]
    stats = attach.download_attachments(iter(files), tmp_path, "x", "d", _10MB)

    assert stats == {"downloaded": 1, "meta_only": 1, "failed": 0}
    att = tmp_path / "channels" / "C001" / "attachments"
    assert (att / "F1.meta.json").exists() and (att / "F1.png").exists()
    assert (att / "F2.meta.json").exists() and not (att / "F2.zip").exists()


def test_download_attachments_skips_over_cap(tmp_path: Path, monkeypatch):
    """A downloadable mime over the size cap stays metadata-only."""
    monkeypatch.setattr(attach, "download_file", _fake_download)
    big = _img("F1")
    big["size"] = 20 * 1024 * 1024
    stats = attach.download_attachments(iter([("C001", "1.0", big)]),
                                        tmp_path, "x", "d", _10MB)
    assert stats["meta_only"] == 1 and stats["downloaded"] == 0
    assert not (tmp_path / "channels" / "C001" / "attachments" / "F1.png").exists()


def test_download_attachments_idempotent(tmp_path: Path, monkeypatch):
    """A file already on disk from a previous run is not re-fetched."""
    att = tmp_path / "channels" / "C001" / "attachments"
    att.mkdir(parents=True)
    (att / "F1.png").write_bytes(b"pre-existing")
    called: list = []

    def fake(url, dst, xoxc, xoxd):
        called.append(url)
        dst.write_bytes(b"replaced")
        return True

    monkeypatch.setattr(attach, "download_file", fake)
    stats = attach.download_attachments(iter([("C001", "1.0", _img("F1"))]),
                                        tmp_path, "x", "d", _10MB)
    assert called == []                                   # no re-download
    assert stats["downloaded"] == 1
    assert (att / "F1.png").read_bytes() == b"pre-existing"

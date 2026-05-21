"""Integration tests for attach.process_channel — full happy path with mock download."""

import json
from pathlib import Path


from slack_log import attach


def test_process_channel_happy_path(tmp_path: Path, monkeypatch):
    """Two files: one image (download), one zip (meta-only)."""
    cdir = tmp_path / "channels" / "C001"
    threads = cdir / "threads"
    threads.mkdir(parents=True)

    msg = {
        "ts": "1.001",
        "files": [
            {"id": "F1", "name": "img.png", "mimetype": "image/png", "size": 1000,
             "filetype": "png", "url_private_download": "https://example.com/F1.png"},
            {"id": "F2", "name": "big.zip", "mimetype": "application/zip", "size": 1000,
             "filetype": "zip", "url_private_download": "https://example.com/F2.zip"},
        ],
    }
    (threads / "1.001.jsonl").write_text(json.dumps(msg) + "\n")

    def fake_download(url, dst, xoxc, xoxd):
        dst.write_bytes(b"fake")
        return True

    monkeypatch.setattr(attach, "download_file", fake_download)
    stats = attach.process_channel(cdir, "xoxc-fake", "xoxd-fake")

    assert stats["downloaded"] == 1  # png
    assert stats["meta_only"] == 1   # zip
    assert stats["failed"] == 0

    # Both meta files should exist; only F1 has the binary
    att = cdir / "attachments"
    assert (att / "F1.meta.json").exists()
    assert (att / "F2.meta.json").exists()
    assert (att / "F1.png").exists()
    assert not (att / "F2.zip").exists()


def test_process_channel_idempotent_skips_existing(tmp_path: Path, monkeypatch):
    """If dst already exists from a previous run, no re-download."""
    cdir = tmp_path / "channels" / "C001"
    threads = cdir / "threads"
    att = cdir / "attachments"
    threads.mkdir(parents=True)
    att.mkdir()

    msg = {"ts": "1.001", "files": [
        {"id": "F1", "name": "img.png", "mimetype": "image/png", "size": 500,
         "filetype": "png", "url_private_download": "https://example.com/F1.png"},
    ]}
    (threads / "1.001.jsonl").write_text(json.dumps(msg) + "\n")
    (att / "F1.png").write_bytes(b"pre-existing")

    download_called = []

    def fake_download(url, dst, xoxc, xoxd):
        download_called.append(url)
        dst.write_bytes(b"replaced")
        return True

    monkeypatch.setattr(attach, "download_file", fake_download)
    stats = attach.process_channel(cdir, "xoxc-fake", "xoxd-fake")

    # download_file must NOT have been called (file already on disk)
    assert download_called == []
    assert stats["downloaded"] == 1
    # Original content untouched
    assert (att / "F1.png").read_bytes() == b"pre-existing"


def test_should_download_size_threshold():
    """Image at exactly 10MB is allowed; just above is not."""
    assert attach.should_download("image/png", 10 * 1024 * 1024) is True
    assert attach.should_download("image/png", 10 * 1024 * 1024 + 1) is False

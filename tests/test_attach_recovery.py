"""TDD round 2: attach must survive an unexpected exception on a single file."""

import json
from pathlib import Path

from slack_log import attach


def test_one_file_exception_does_not_stop_channel(tmp_path: Path, monkeypatch):
    """When download_file raises an unexpected exception on one file,
    the others in the same channel must still be processed."""
    cdir = tmp_path / "channels" / "C001"
    threads = cdir / "threads"
    threads.mkdir(parents=True)

    msg = {
        "ts": "1.001",
        "files": [
            {"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 100,
             "filetype": "png", "url_private_download": "https://example.com/F1.png"},
            {"id": "F2", "name": "b.png", "mimetype": "image/png", "size": 100,
             "filetype": "png", "url_private_download": "https://example.com/F2.png"},
        ],
    }
    (threads / "1.001.jsonl").write_text(json.dumps(msg) + "\n")

    calls = []

    def fake_download(url, dst, xoxc, xoxd):
        calls.append(url)
        if "F1.png" in url:
            # Simulate an unexpected exception class that the inner try/except
            # in download_file does NOT catch (e.g. SSL / disk full / unknown).
            raise RuntimeError("simulated unexpected failure")
        dst.write_bytes(b"fake png bytes")
        return True

    monkeypatch.setattr(attach, "download_file", fake_download)
    stats = attach.process_channel(cdir, "xoxc-fake", "xoxd-fake")

    assert len(calls) == 2, f"both files should be attempted, got {calls}"
    assert stats["downloaded"] == 1, f"F2 should be downloaded: {stats}"
    assert stats["failed"] == 1, f"F1 should count as failed: {stats}"

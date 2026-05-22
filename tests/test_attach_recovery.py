"""attach must survive an unexpected exception on a single file."""

from pathlib import Path

from slack_log.pipeline import attach

_10MB = 10 * 1024 * 1024


def test_one_file_exception_does_not_stop_the_walk(tmp_path: Path, monkeypatch):
    """When download_file raises an exception its inner try/except doesn't catch
    (SSL, disk full, …) on one file, the rest of the walk still runs."""
    files = [
        ("C001", "1.0", {"id": "F1", "mimetype": "image/png", "size": 100,
                         "filetype": "png", "url_private_download": "https://x/F1.png"}),
        ("C001", "1.0", {"id": "F2", "mimetype": "image/png", "size": 100,
                         "filetype": "png", "url_private_download": "https://x/F2.png"}),
    ]
    calls: list = []

    def fake_download(url, dst, xoxc, xoxd):
        calls.append(url)
        if "F1.png" in url:
            raise RuntimeError("simulated unexpected failure")
        dst.write_bytes(b"ok")
        return True

    monkeypatch.setattr(attach, "download_file", fake_download)
    stats = attach.download_attachments(iter(files), tmp_path, "x", "d", _10MB)

    assert len(calls) == 2            # both files attempted
    assert stats["downloaded"] == 1   # F2 succeeded
    assert stats["failed"] == 1       # F1 recorded as failed, not raised

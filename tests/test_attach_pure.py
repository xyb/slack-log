"""Unit tests for attach pure functions (no network)."""

import pytest

from slack_log.pipeline.attach import should_download

_10MB = 10 * 1024 * 1024


@pytest.mark.parametrize("mimetype,size,expected", [
    ("image/png", 100_000, True),
    ("image/jpeg", 1_000_000, True),
    ("image/png", _10MB + 1, False),           # over the cap
    ("text/plain", 1_000, True),
    ("application/json", 4_000_000, True),
    ("application/pdf", 5_000_000, True),
    ("application/zip", 100, False),           # archives never download
    ("application/x-tar", 100, False),
    ("video/mp4", 100, False),                 # media never downloads
    ("audio/mpeg", 100, False),
    ("application/octet-stream", 100, False),  # unknown mime
    ("", 100, False),
    (None, 100, False),
])
def test_should_download(mimetype, size, expected):
    assert should_download(mimetype, size, _10MB) is expected


def test_should_download_cap_is_configurable():
    """The same file: kept under a generous cap, skipped under a tight one."""
    eight_mb = 8 * 1024 * 1024
    assert should_download("image/png", eight_mb, _10MB) is True
    assert should_download("image/png", eight_mb, 5 * 1024 * 1024) is False


def test_should_download_threshold_is_inclusive():
    assert should_download("image/png", _10MB, _10MB) is True
    assert should_download("image/png", _10MB + 1, _10MB) is False

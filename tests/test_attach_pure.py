"""Unit tests for attach.py pure functions (no network)."""

import pytest

from slack_log.attach import should_download


@pytest.mark.parametrize("mimetype,size,expected", [
    ("image/png", 100_000, True),
    ("image/jpeg", 1_000_000, True),
    ("image/png", 11_000_000, False),  # over 10MB
    ("text/plain", 1_000, True),
    ("text/csv", 4_000_000, True),
    ("text/plain", 6_000_000, False),   # over 5MB
    ("application/pdf", 5_000_000, True),
    ("application/pdf", 21_000_000, False),
    ("application/zip", 100, False),     # zip never downloaded
    ("video/mp4", 100, False),           # video never downloaded
    ("audio/mpeg", 100, False),
    ("application/octet-stream", 100, False),  # unknown mime
    ("", 100, False),
    (None, 100, False),
])
def test_should_download(mimetype, size, expected):
    assert should_download(mimetype, size) is expected

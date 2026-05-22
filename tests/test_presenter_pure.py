"""Unit tests for presenter.py pure functions (no IO).

Functions covered:
- format_bytes, ts_to_human
- render_user, render_channel, kind_of
- render_files (image / link / remote)
- render_attachments (color hex normalization, field mapping)

Slack text processing (emojize / apply_mrkdwn / expand_mentions) moved to
core.text — see test_core_text.py.
"""

from pathlib import Path

import pytest

from slack_log.web import presenter


# --- format_bytes ---

@pytest.mark.parametrize("n,expected", [
    (0, "?"),
    (None, "?"),
    (100, "100 B"),
    (1024, "1 KB"),
    (1536, "1.5 KB"),
    (1024 * 1024, "1 MB"),
    (1024 * 1024 * 1024, "1 GB"),
])
def test_format_bytes(n, expected):
    assert presenter.format_bytes(n) == expected


# --- ts_to_human ---

def test_ts_to_human_valid():
    assert presenter.ts_to_human("1700000000.123456").startswith("2023-11-")  # whatever local TZ


def test_ts_to_human_invalid_returns_original():
    assert presenter.ts_to_human("not-a-ts") == "not-a-ts"


# --- render_user ---

def test_render_user_uses_display_name_first():
    users = {"U1": {"display_name": "Alice", "real_name": "Alice Liddell", "name": "alice"}}
    assert presenter.render_user("U1", users) == "Alice"


def test_render_user_falls_back_to_real_name():
    users = {"U1": {"display_name": "", "real_name": "Alice Liddell", "name": "alice"}}
    assert presenter.render_user("U1", users) == "Alice Liddell"


def test_render_user_falls_back_to_name_then_uid():
    assert presenter.render_user("U1", {"U1": {"name": "alice"}}) == "alice"
    assert presenter.render_user("U99", {}) == "U99"
    assert presenter.render_user(None, {}) == "(unknown)"


# --- render_channel ---

def test_render_channel_uses_name():
    assert presenter.render_channel("C1", {"C1": {"name": "general"}}) == "general"


def test_render_channel_falls_back_to_id():
    assert presenter.render_channel("C1", {}) == "C1"


# --- kind_of ---

def test_kind_of():
    channels = {
        "C1": {"is_im": False, "is_mpim": False},
        "D1": {"is_im": True},
        "G1": {"is_mpim": True},
    }
    assert presenter.kind_of("C1", channels) == "channel"
    assert presenter.kind_of("D1", channels) == "dm"
    assert presenter.kind_of("G1", channels) == "mpim"
    assert presenter.kind_of("UNKNOWN", channels) == "channel"  # default


# --- render_files ---

def test_render_files_image_when_local_exists(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "F1.png").write_bytes(b"fake")
    files = [{"id": "F1", "name": "a.png", "mimetype": "image/png",
              "size": 12345, "filetype": "png", "permalink": "https://x"}]
    out = presenter.render_files(files, att)
    assert len(out) == 1
    assert out[0]["kind"] == "image"
    assert out[0]["rel"] == "../attachments/F1.png"
    assert out[0]["size_human"] == "12.1 KB"


def test_render_files_remote_when_local_missing(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    files = [{"id": "F2", "name": "big.zip", "mimetype": "application/zip",
              "size": 99999, "filetype": "zip", "permalink": "https://slack/F2"}]
    out = presenter.render_files(files, att)
    assert out[0]["kind"] == "remote"
    assert out[0]["href"] == "https://slack/F2"


def test_render_files_link_for_non_image(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "F3.pdf").write_bytes(b"%PDF")
    files = [{"id": "F3", "name": "a.pdf", "mimetype": "application/pdf",
              "size": 100, "filetype": "pdf"}]
    out = presenter.render_files(files, att)
    assert out[0]["kind"] == "link"


def test_render_files_skip_missing_id(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    out = presenter.render_files([{"name": "noid"}], att)
    assert out == []


# --- render_attachments ---

def test_render_attachments_hex_color_normalized():
    out = presenter.render_attachments(
        [{"color": "26a69a", "title": "T", "title_link": "https://x", "text": "hi"}],
        {}, {})
    assert out[0]["color"] == "#26a69a"


def test_render_attachments_default_color_when_missing():
    out = presenter.render_attachments([{"title": "T"}], {}, {})
    assert out[0]["color"].startswith("#")


def test_render_attachments_text_expanded():
    out = presenter.render_attachments(
        [{"text": "see <https://x.com> :heart:"}],
        {}, {})
    assert "<a " in out[0]["text_html"]
    assert "❤️" in out[0]["text_html"]


def test_render_attachments_skip_non_dict():
    """Defensive: random junk in attachments list shouldn't crash."""
    out = presenter.render_attachments(["not a dict", None, 42], {}, {})
    assert out == []

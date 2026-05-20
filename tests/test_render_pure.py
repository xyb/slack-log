"""Unit tests for render.py pure functions (no IO).

Functions covered:
- format_bytes, ts_to_human
- render_user, render_channel, kind_of
- apply_mrkdwn (bold/italic/code/strike/quote + word boundary)
- expand_mentions (user/channel/link/broadcast/emoji + inside_anchor)
- expand_for_preview (link <a> downgraded to <span>)
- render_files (image / link / remote)
- render_attachments (color hex normalization, field mapping)
- emojize
"""

from pathlib import Path

import pytest

from slack_log import render


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
    assert render.format_bytes(n) == expected


# --- ts_to_human ---

def test_ts_to_human_valid():
    assert render.ts_to_human("1700000000.123456").startswith("2023-11-")  # whatever local TZ


def test_ts_to_human_invalid_returns_original():
    assert render.ts_to_human("not-a-ts") == "not-a-ts"


# --- render_user ---

def test_render_user_uses_display_name_first():
    users = {"U1": {"display_name": "Alice", "real_name": "Alice Liddell", "name": "alice"}}
    assert render.render_user("U1", users) == "Alice"


def test_render_user_falls_back_to_real_name():
    users = {"U1": {"display_name": "", "real_name": "Alice Liddell", "name": "alice"}}
    assert render.render_user("U1", users) == "Alice Liddell"


def test_render_user_falls_back_to_name_then_uid():
    assert render.render_user("U1", {"U1": {"name": "alice"}}) == "alice"
    assert render.render_user("U99", {}) == "U99"
    assert render.render_user(None, {}) == "(unknown)"


# --- render_channel ---

def test_render_channel_uses_name():
    assert render.render_channel("C1", {"C1": {"name": "general"}}) == "general"


def test_render_channel_falls_back_to_id():
    assert render.render_channel("C1", {}) == "C1"


# --- kind_of ---

def test_kind_of():
    channels = {
        "C1": {"is_im": False, "is_mpim": False},
        "D1": {"is_im": True},
        "G1": {"is_mpim": True},
    }
    assert render.kind_of("C1", channels) == "channel"
    assert render.kind_of("D1", channels) == "dm"
    assert render.kind_of("G1", channels) == "mpim"
    assert render.kind_of("UNKNOWN", channels) == "channel"  # default


# --- emojize ---

def test_emojize_known_shortcode():
    assert render.emojize(":heart:") == "❤️"
    assert render.emojize(":thumbsup:") == "👍"


def test_emojize_unknown_shortcode_left_alone():
    assert render.emojize(":cool-doge:") == ":cool-doge:"


def test_emojize_empty():
    assert render.emojize("") == ""


# --- apply_mrkdwn ---

@pytest.mark.parametrize("text,expected", [
    ("*bold*", "<strong>bold</strong>"),
    ("_italic_", "<em>italic</em>"),
    ("`code`", "<code>code</code>"),
    ("~strike~", "<s>strike</s>"),
    ("> quote", "<blockquote>quote</blockquote>"),
])
def test_apply_mrkdwn_simple(text, expected):
    assert render.apply_mrkdwn(text) == expected


def test_apply_mrkdwn_snake_case_not_italicized():
    """foo_bar_baz must not become foo<em>bar</em>baz."""
    out = render.apply_mrkdwn("foo_bar_baz")
    assert "<em>" not in out
    assert out == "foo_bar_baz"


def test_apply_mrkdwn_inline_within_text():
    out = render.apply_mrkdwn("hello *world* and `code` and _yes_")
    assert "<strong>world</strong>" in out
    assert "<code>code</code>" in out
    assert "<em>yes</em>" in out


def test_apply_mrkdwn_empty():
    assert render.apply_mrkdwn("") == ""


# --- expand_mentions ---

def test_expand_mentions_user():
    users = {"U1": {"display_name": "Alice"}}
    assert "@Alice" in render.expand_mentions("<@U1>", users, {})


def test_expand_mentions_user_with_alias():
    out = render.expand_mentions("<@U1|aliased>", {}, {})
    assert "@aliased" in out


def test_expand_mentions_channel():
    channels = {"C1": {"name": "general"}}
    out = render.expand_mentions("<#C1>", {}, channels)
    assert "#general" in out


def test_expand_mentions_link_with_label():
    out = render.expand_mentions("<https://x.com|click here>", {}, {})
    assert 'href="https://x.com"' in out
    assert ">click here</a>" in out


def test_expand_mentions_bare_link():
    out = render.expand_mentions("<https://x.com>", {}, {})
    assert 'href="https://x.com"' in out


def test_expand_mentions_broadcast():
    for word in ("here", "channel", "everyone"):
        out = render.expand_mentions(f"<!{word}>", {}, {})
        assert f"@{word}" in out
        assert "mention-broadcast" in out


def test_expand_mentions_emoji_at_end():
    """Pipeline applies mrkdwn + emojize after Slack syntax replacement."""
    out = render.expand_mentions("hello :heart:", {}, {})
    assert "❤️" in out


# --- expand_for_preview ---

def test_expand_for_preview_downgrades_links_to_span():
    """Preview is inside <a class="thread-link">, so no nested <a>."""
    out = render.expand_for_preview("<https://x.com>", {}, {})
    assert "<a " not in out
    assert '<span class="ext-link">' in out


# --- render_files ---

def test_render_files_image_when_local_exists(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "F1.png").write_bytes(b"fake")
    files = [{"id": "F1", "name": "a.png", "mimetype": "image/png",
              "size": 12345, "filetype": "png", "permalink": "https://x"}]
    out = render.render_files(files, tmp_path)
    assert len(out) == 1
    assert out[0]["kind"] == "image"
    assert out[0]["rel"] == "../attachments/F1.png"
    assert out[0]["size_human"] == "12.1 KB"


def test_render_files_remote_when_local_missing(tmp_path: Path):
    (tmp_path / "attachments").mkdir()
    files = [{"id": "F2", "name": "big.zip", "mimetype": "application/zip",
              "size": 99999, "filetype": "zip", "permalink": "https://slack/F2"}]
    out = render.render_files(files, tmp_path)
    assert out[0]["kind"] == "remote"
    assert out[0]["href"] == "https://slack/F2"


def test_render_files_link_for_non_image(tmp_path: Path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "F3.pdf").write_bytes(b"%PDF")
    files = [{"id": "F3", "name": "a.pdf", "mimetype": "application/pdf",
              "size": 100, "filetype": "pdf"}]
    out = render.render_files(files, tmp_path)
    assert out[0]["kind"] == "link"


def test_render_files_skip_missing_id(tmp_path: Path):
    (tmp_path / "attachments").mkdir()
    out = render.render_files([{"name": "noid"}], tmp_path)
    assert out == []


# --- render_attachments ---

def test_render_attachments_hex_color_normalized():
    out = render.render_attachments(
        [{"color": "26a69a", "title": "T", "title_link": "https://x", "text": "hi"}],
        {}, {})
    assert out[0]["color"] == "#26a69a"


def test_render_attachments_default_color_when_missing():
    out = render.render_attachments([{"title": "T"}], {}, {})
    assert out[0]["color"].startswith("#")


def test_render_attachments_text_expanded():
    out = render.render_attachments(
        [{"text": "see <https://x.com> :heart:"}],
        {}, {})
    assert "<a " in out[0]["text_html"]
    assert "❤️" in out[0]["text_html"]


def test_render_attachments_skip_non_dict():
    """Defensive: random junk in attachments list shouldn't crash."""
    out = render.render_attachments(["not a dict", None, 42], {}, {})
    assert out == []

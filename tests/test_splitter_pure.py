"""Unit tests for splitter.py pure functions (no SQLite).

Functions covered:
- make_preview: strips Slack syntax, resolves user/channel names, truncates,
  newline-to-space, never leaves a dangling `<url` opener that would break HTML.
"""


from slack_log.splitter import make_preview


def test_make_preview_empty():
    assert make_preview("") == ""
    assert make_preview(None) == ""


def test_make_preview_strips_user_mention_uses_display_name():
    users = {"U1": {"display_name": "Alice"}}
    assert make_preview("hi <@U1>", users) == "hi @Alice"


def test_make_preview_user_mention_alias_overrides_users_dict():
    out = make_preview("hi <@U1|aliased>", {"U1": {"display_name": "Real"}})
    assert "@aliased" in out
    assert "Real" not in out


def test_make_preview_user_mention_unknown_uid_falls_back():
    assert make_preview("hi <@U99>", {}) == "hi @U99"


def test_make_preview_channel_mention():
    channels = {"C1": {"name": "general"}}
    assert make_preview("see <#C1>", {}, channels) == "see #general"


def test_make_preview_link_with_label_keeps_label():
    assert make_preview("<https://x.com|click here>") == "click here"


def test_make_preview_bare_link_keeps_url():
    assert make_preview("<https://x.com>") == "https://x.com"


def test_make_preview_broadcast():
    assert make_preview("<!here>") == "@here"
    assert make_preview("<!channel>") == "@channel"


def test_make_preview_newline_to_space():
    assert make_preview("line1\nline2") == "line1 line2"


def test_make_preview_truncates_to_100_chars_default():
    long = "x" * 500
    assert len(make_preview(long)) == 100


def test_make_preview_custom_max_len():
    assert len(make_preview("x" * 500, max_len=20)) == 20


def test_make_preview_no_dangling_lt_after_truncation():
    """A long <url> that would be cut mid-tag must be fully stripped first
    so the preview never ends with an unclosed `<` that breaks HTML parsing."""
    text = "prefix <https://example.com/very/long/path/that/goes/on/and/on/and/on/until/we/exceed/the/cap>"
    out = make_preview(text)
    # The Slack <url> is stripped to a plain URL string before truncation,
    # so there should be no leftover `<` from the Slack syntax in the output.
    assert "<https" not in out
    assert "<@" not in out
    assert "<#" not in out

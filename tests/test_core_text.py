"""Unit tests for slack_log.core.text — Slack text processing.

Covers the three transforms and their helpers:
- make_preview     : strips Slack syntax, resolves names, truncates safely
- emojize          : :shortcode: → emoji
- apply_mrkdwn     : Slack mrkdwn → HTML (bold/italic/code/strike/quote)
- expand_mentions  : Slack <...> syntax → HTML fragment
- expand_for_preview : same, but external links downgraded to <span>
"""

import pytest

from slack_log.core.text import (
    apply_mrkdwn,
    channel_name,
    display_name,
    emojize,
    expand_for_preview,
    expand_mentions,
    join_cjk,
    make_preview,
    normalize_text,
    split_cjk,
)


# --- make_preview ---

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
    assert "<https" not in out
    assert "<@" not in out
    assert "<#" not in out


# --- emojize ---

def test_emojize_known_shortcode():
    assert emojize(":heart:") == "❤️"
    assert emojize(":thumbsup:") == "👍"


def test_emojize_unknown_shortcode_left_alone():
    assert emojize(":cool-doge:") == ":cool-doge:"


def test_emojize_empty():
    assert emojize("") == ""


# --- apply_mrkdwn ---

@pytest.mark.parametrize("text,expected", [
    ("*bold*", "<strong>bold</strong>"),
    ("_italic_", "<em>italic</em>"),
    ("`code`", "<code>code</code>"),
    ("~strike~", "<s>strike</s>"),
    ("> quote", "<blockquote>quote</blockquote>"),
])
def test_apply_mrkdwn_simple(text, expected):
    assert apply_mrkdwn(text) == expected


def test_apply_mrkdwn_snake_case_not_italicized():
    """foo_bar_baz must not become foo<em>bar</em>baz."""
    out = apply_mrkdwn("foo_bar_baz")
    assert "<em>" not in out
    assert out == "foo_bar_baz"


def test_apply_mrkdwn_inline_within_text():
    out = apply_mrkdwn("hello *world* and `code` and _yes_")
    assert "<strong>world</strong>" in out
    assert "<code>code</code>" in out
    assert "<em>yes</em>" in out


def test_apply_mrkdwn_empty():
    assert apply_mrkdwn("") == ""


# --- expand_mentions ---

def test_expand_mentions_user():
    users = {"U1": {"display_name": "Alice"}}
    assert "@Alice" in expand_mentions("<@U1>", users, {})


def test_expand_mentions_user_with_alias():
    out = expand_mentions("<@U1|aliased>", {}, {})
    assert "@aliased" in out


def test_expand_mentions_channel():
    channels = {"C1": {"name": "general"}}
    out = expand_mentions("<#C1>", {}, channels)
    assert "#general" in out


def test_expand_mentions_link_with_label():
    out = expand_mentions("<https://x.com|click here>", {}, {})
    assert 'href="https://x.com"' in out
    assert ">click here</a>" in out


def test_expand_mentions_bare_link():
    out = expand_mentions("<https://x.com>", {}, {})
    assert 'href="https://x.com"' in out


def test_expand_mentions_broadcast():
    for word in ("here", "channel", "everyone"):
        out = expand_mentions(f"<!{word}>", {}, {})
        assert f"@{word}" in out
        assert "mention-broadcast" in out


def test_expand_mentions_emoji_at_end():
    """Pipeline applies mrkdwn + emojize after Slack syntax replacement."""
    out = expand_mentions("hello :heart:", {}, {})
    assert "❤️" in out


# --- expand_for_preview ---

def test_expand_for_preview_downgrades_links_to_span():
    """Preview is inside <a class="thread-link">, so no nested <a>."""
    out = expand_for_preview("<https://x.com>", {}, {})
    assert "<a " not in out
    assert '<span class="ext-link">' in out


def test_expand_for_preview_empty():
    assert expand_for_preview("", {}, {}) == ""


def test_expand_mentions_channel_alias():
    assert "#aliased" in expand_mentions("<#C1|aliased>", {}, {})


def test_make_preview_channel_alias():
    assert make_preview("see <#C1|named>", {}, {}) == "see #named"


# --- split_cjk / join_cjk ---

def test_split_cjk_inserts_spaces_between_ideographs():
    assert split_cjk("发布") == " 发  布 "


def test_join_cjk_collapses_space_between_cjk_chars():
    # join_cjk only removes whitespace *between* two CJK chars (for snippet
    # display); it does not trim leading/trailing space.
    assert join_cjk("发 布") == "发布"
    assert join_cjk("今 天 发 布") == "今天发布"


def test_join_cjk_keeps_ascii_spacing():
    assert join_cjk("hello world") == "hello world"


def test_join_cjk_empty():
    assert join_cjk("") == ""


def test_split_cjk_leaves_ascii_untouched():
    assert split_cjk("hello world") == "hello world"


# --- display_name / channel_name ---

def test_display_name_priority_and_fallbacks():
    users = {"U1": {"display_name": "Alice", "real_name": "A L", "name": "al"}}
    assert display_name("U1", users) == "Alice"
    assert display_name("U1", {"U1": {"real_name": "A L"}}) == "A L"
    assert display_name("U1", {"U1": {"name": "al"}}) == "al"
    assert display_name("U9", {}) == "U9"        # unknown uid → uid itself
    assert display_name(None, {}) == ""          # no uid → empty


def test_channel_name_falls_back_to_cid():
    assert channel_name("C1", {"C1": {"name": "general"}}) == "general"
    assert channel_name("C9", {}) == "C9"


# --- normalize_text (search-index normalization) ---

def test_normalize_text_empty():
    assert normalize_text("", {}, {}) == ""


def test_normalize_text_resolves_user_and_channel_mentions():
    users = {"U1": {"display_name": "Alice"}}
    channels = {"C1": {"name": "general"}}
    out = normalize_text("hi <@U1> in <#C1>", users, channels)
    assert "@Alice" in out
    assert "#general" in out


def test_normalize_text_mention_aliases_win():
    out = normalize_text("<@U1|bob> <#C1|chan>", {"U1": {"display_name": "Real"}}, {})
    assert "@bob" in out and "#chan" in out
    assert "Real" not in out


def test_normalize_text_strips_link_wrappers_and_emojizes():
    out = normalize_text("see <https://x.com|docs> :heart:", {}, {})
    assert "docs" in out and "https://x.com" in out
    assert "❤" in out


def test_normalize_text_splits_cjk_for_indexing():
    # CJK runs are split char-by-char so FTS5 unicode61 tokenizes them
    assert normalize_text("发布", {}, {}) == split_cjk("发布")

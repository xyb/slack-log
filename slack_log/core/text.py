"""Slack message text processing — shared by splitter / indexer / render.

Three distinct transforms over Slack's `text` field, all built on the same set
of `<...>` syntax regexes:

  make_preview    → plain text (channel index previews)
  normalize_text  → search-normalized text (FTS5 index)
  expand_mentions → HTML fragment (thread rendering)

Keeping the regexes and the CJK split/join helpers in one place stops the three
pipeline stages from drifting apart.
"""

import html
import re

import emoji

# Slack inline syntax. Slack pre-escapes literal `<`/`>`/`&` in message text,
# so a leading `<` in the raw text can only be one of these constructs.
USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
CHANNEL_MENTION = re.compile(r"<#([C][A-Z0-9]+)(?:\|([^>]+))?>")
LINK_WITH_LABEL = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
LINK_BARE = re.compile(r"<(https?://[^>\s]+)>")
# Broadcast mention. The bare form is what splitter/render historically
# matched; normalize_text additionally tolerates a `|label` suffix. Kept as two
# constants on purpose — unifying them would change matching on `<!here|...>`.
BROADCAST = re.compile(r"<!(here|channel|everyone)>")
SPECIAL_MENTION = re.compile(r"<!(here|channel|everyone)(?:\|[^>]+)?>")

# CJK ideographs — unicode61 does not break runs of them, so the indexer
# inserts spaces between them and joins them back for display.
CJK_CHAR = re.compile(r"[㐀-鿿豈-﫿]")
JOIN_CJK = re.compile(r"(?<=[㐀-鿿豈-﫿])\s+(?=[㐀-鿿豈-﫿])")


def emojize(text: str) -> str:
    """:heart: → ❤️. A Slack custom emoji the library doesn't know is left as-is."""
    if not text:
        return ""
    return emoji.emojize(text, language="alias")


def split_cjk(text: str) -> str:
    """Insert spaces between CJK ideographs so unicode61 tokenizes them one-by-one.

    Why: unicode61 has no separator between CJK chars, so an entire Chinese run
    becomes one token. Searching "发布" inside "今天发布新版本" then misses.
    Pre-splitting at index time makes "发布" a 2-token phrase that matches.
    """
    return CJK_CHAR.sub(lambda m: " " + m.group(0) + " ", text)


def join_cjk(text: str) -> str:
    """Inverse of split_cjk for display: collapse the space between two CJK chars."""
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = JOIN_CJK.sub("", text)
    return text


def make_preview(text: str, users: dict | None = None, channels: dict | None = None,
                 max_len: int = 100) -> str:
    """Strip Slack syntax + truncate to max_len. Resolves mention display names
    (when a users dict is given). Stripping happens before truncation so the
    preview never ends on a dangling `<` that would break HTML parsing."""
    if not text:
        return ""
    users = users or {}
    channels = channels or {}

    def user_repl(m):
        uid, alias = m.group(1), m.group(2)
        if alias:
            return "@" + alias
        u = users.get(uid) or {}
        return "@" + (u.get("display_name") or u.get("real_name") or u.get("name") or uid)

    def chan_repl(m):
        cid, alias = m.group(1), m.group(2)
        if alias:
            return "#" + alias
        c = channels.get(cid) or {}
        return "#" + (c.get("name") or cid)

    text = USER_MENTION.sub(user_repl, text)
    text = CHANNEL_MENTION.sub(chan_repl, text)
    text = LINK_WITH_LABEL.sub(lambda m: m.group(2), text)
    text = LINK_BARE.sub(lambda m: m.group(1), text)
    text = BROADCAST.sub(lambda m: "@" + m.group(1), text)
    text = text.replace("\n", " ").strip()
    return text[:max_len]


def display_name(uid: str | None, users: dict) -> str:
    if not uid:
        return ""
    u = users.get(uid) or {}
    return u.get("display_name") or u.get("real_name") or u.get("name") or uid


def channel_name(cid: str, channels: dict) -> str:
    return (channels.get(cid) or {}).get("name") or cid


def normalize_text(text: str, users: dict, channels: dict) -> str:
    """Resolve mention uids / channel cids, strip Slack <link> wrappers, emojize."""
    if not text:
        return ""

    def repl_user(m: re.Match) -> str:
        label = m.group(2)
        if label:
            return f"@{label}"
        return f"@{display_name(m.group(1), users)}"

    def repl_channel(m: re.Match) -> str:
        label = m.group(2)
        if label:
            return f"#{label}"
        return f"#{channel_name(m.group(1), channels)}"

    text = USER_MENTION.sub(repl_user, text)
    text = CHANNEL_MENTION.sub(repl_channel, text)
    text = SPECIAL_MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = LINK_WITH_LABEL.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = LINK_BARE.sub(lambda m: m.group(1), text)
    text = emoji.emojize(text, language="alias")
    return split_cjk(text)


def apply_mrkdwn(text: str) -> str:
    """Slack mrkdwn → HTML (subset)."""
    if not text:
        return ""
    # inline code 先（防内部 * 被改）
    text = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
    # 块 quote
    text = re.sub(r"(?m)^&gt;\s*(.+)$", r"<blockquote>\1</blockquote>", text)
    text = re.sub(r"(?m)^>\s*(.+)$", r"<blockquote>\1</blockquote>", text)
    # bold *xxx*
    text = re.sub(
        r"(?<![a-zA-Z0-9_*])\*([^\s*][^*\n]*?[^\s*]|\S)\*(?![a-zA-Z0-9_*])",
        r"<strong>\1</strong>",
        text,
    )
    # italic _xxx_ 避开 snake_case
    text = re.sub(
        r"(?<![a-zA-Z0-9_])_([^\s_][^_\n]*?[^\s_]|\S)_(?![a-zA-Z0-9_])",
        r"<em>\1</em>",
        text,
    )
    # strike ~xxx~
    text = re.sub(
        r"(?<![a-zA-Z0-9~])~([^\s~][^~\n]*?[^\s~]|\S)~(?![a-zA-Z0-9~])",
        r"<s>\1</s>",
        text,
    )
    return text


def expand_mentions(text: str, users: dict, channels: dict) -> str:
    """Slack 特殊语法 → HTML 片段。

    Slack API 返回的 text 里普通的 `<`/`>`/`&` 已被 escape 成 entity，所以原始
    text 里以 `<` 开头的只可能是 Slack 自己的特殊语法（mention/link/channel/broadcast）。
    我们替换这些特殊语法为 HTML span/a，其他字符原样保留。模板里用 `|safe` 输出。
    """
    if not text:
        return ""

    def user_repl(m):
        uid, alias = m.group(1), m.group(2)
        if alias:
            name = alias
        else:
            u = users.get(uid) or {}
            name = u.get("display_name") or u.get("real_name") or u.get("name") or uid
        return f'<span class="mention mention-user">@{html.escape(name)}</span>'

    def chan_repl(m):
        cid, alias = m.group(1), m.group(2)
        if alias:
            name = alias
        else:
            c = channels.get(cid) or {}
            name = c.get("name") or cid
        return f'<span class="mention mention-channel">#{html.escape(name)}</span>'

    def link_with_label_repl(m):
        url, label = m.group(1), m.group(2)
        return f'<a class="ext-link" href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'

    def link_bare_repl(m):
        url = m.group(1)
        url_esc = html.escape(url)
        return f'<a class="ext-link" href="{url_esc}" target="_blank" rel="noopener">{url_esc}</a>'

    text = USER_MENTION.sub(user_repl, text)
    text = CHANNEL_MENTION.sub(chan_repl, text)
    text = LINK_WITH_LABEL.sub(link_with_label_repl, text)
    text = LINK_BARE.sub(link_bare_repl, text)
    text = BROADCAST.sub(lambda m: f'<span class="mention mention-broadcast">@{m.group(1)}</span>', text)
    text = apply_mrkdwn(text)
    text = emojize(text)
    return text


def expand_for_preview(text: str, users: dict, channels: dict) -> str:
    """Preview 在父 `<a>` 内部（channel index 的 thread-link 是 `<a>`），
    所以不能输出 `<a>` (HTML 不允许 `<a>` 嵌套 `<a>` —— 浏览器自动闭合外层
    破坏布局)。外链降级成 `<span>`，其他跟 expand_mentions 一样。
    """
    if not text:
        return ""
    # 复用 expand_mentions 但跑完后把生成的 ext-link <a> 替换成 <span>
    rendered = expand_mentions(text, users, channels)
    rendered = re.sub(
        r'<a class="ext-link"[^>]*>([^<]*)</a>',
        r'<span class="ext-link">\1</span>',
        rendered,
    )
    return rendered

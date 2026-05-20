"""Integration tests: render.main() against a minimal data/ directory."""

import json
import sys
from pathlib import Path

import pytest

from slack_log import render


@pytest.fixture
def minimal_data(tmp_path: Path) -> Path:
    """Create a minimal data/ tree that render.main() can ingest."""
    data = tmp_path / "data"
    cdir = data / "channels" / "C001"
    threads = cdir / "threads"
    threads.mkdir(parents=True)

    # one valid thread with one message
    (threads / "1700000000.000001.jsonl").write_text(
        json.dumps({
            "ts": "1700000000.000001",
            "user": "U001",
            "text": "hello *world*",
            "reactions": [{"name": "heart", "count": 1, "users": ["U001"]}],
        }) + "\n"
    )
    (cdir / "index.jsonl").write_text(
        json.dumps({
            "thread_ts": "1700000000.000001",
            "first_ts": "1700000000.000001",
            "first_user": "U001",
            "first_text_preview": "hello",
            "latest_reply_ts": "1700000000.000001",
            "reply_count": 0,
            "msg_count": 1,
            "is_thread": False,
            "has_files": False,
            "participants": ["U001"],
        }) + "\n"
    )

    (data / "users.json").write_text(json.dumps({
        "U001": {"name": "alice", "real_name": "Alice", "display_name": "Alice",
                 "image_48": "https://x/alice_48.png", "image_24": "https://x/alice_24.png"},
    }))
    (data / "channels.json").write_text(json.dumps({
        "C001": {"name": "general", "is_im": False, "is_mpim": False,
                 "is_channel": True, "is_private": False, "is_archived": False,
                 "members": [], "other_uid": None},
    }))
    return data


def test_main_renders_full_site(minimal_data: Path, tmp_path: Path, monkeypatch):
    html = tmp_path / "html"
    templates = Path(render.__file__).parent / "templates"

    monkeypatch.setattr(sys, "argv", [
        "render.py",
        "--data", str(minimal_data),
        "--html", str(html),
        "--templates", str(templates),
    ])
    render.main()

    # Global index lists the channel
    global_index = (html / "index.html").read_text()
    assert "general" in global_index

    # Channel index page exists and mentions the thread preview
    channel_index = (html / "channels" / "C001" / "index.html").read_text()
    assert "general" in channel_index
    assert "hello" in channel_index

    # Thread page exists with: ref id anchor, message text, mrkdwn bold,
    # avatar, reaction emoji
    thread_html = (html / "channels" / "C001" / "threads" / "1700000000.000001.html").read_text()
    assert 'id="msg-1700000000.000001"' in thread_html
    assert "<strong>world</strong>" in thread_html  # mrkdwn
    assert "❤️" in thread_html                      # emoji
    assert "alice_48.png" in thread_html             # avatar


def test_main_with_include_filter(minimal_data: Path, tmp_path: Path, monkeypatch):
    """--include=dm should skip the only channel (which is is_channel=True)."""
    html = tmp_path / "html"
    templates = Path(render.__file__).parent / "templates"

    monkeypatch.setattr(sys, "argv", [
        "render.py",
        "--data", str(minimal_data),
        "--html", str(html),
        "--templates", str(templates),
        "--include", "dm",
    ])
    render.main()

    # Channel page should NOT have been written
    assert not (html / "channels" / "C001" / "index.html").exists()
    # Global index also drops the channel section
    global_index = (html / "index.html").read_text()
    assert "📢 Channels" not in global_index

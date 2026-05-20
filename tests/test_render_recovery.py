"""TDD round 3: render must keep going if one thread can't be rendered."""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from slack_log import render


def test_one_broken_thread_does_not_stop_channel(tmp_path: Path):
    """A thread whose jsonl can't be parsed must not stop sibling threads from rendering."""
    cdir = tmp_path / "data" / "channels" / "C001"
    threads = cdir / "threads"
    threads.mkdir(parents=True)

    # Valid thread
    (threads / "T1.jsonl").write_text(
        json.dumps({"ts": "T1", "user": "U1", "text": "hi"}) + "\n"
    )
    # Broken thread — bad json line
    (threads / "T2.jsonl").write_text("not-valid-json{{{\n")

    (cdir / "index.jsonl").write_text(
        json.dumps({
            "thread_ts": "T1", "first_ts": "T1", "first_user": "U1",
            "first_text_preview": "hi", "latest_reply_ts": "T1",
            "reply_count": 0, "msg_count": 1, "is_thread": False,
            "has_files": False, "participants": ["U1"],
        }) + "\n" +
        json.dumps({
            "thread_ts": "T2", "first_ts": "T2", "first_user": "U1",
            "first_text_preview": "broken", "latest_reply_ts": "T2",
            "reply_count": 0, "msg_count": 1, "is_thread": False,
            "has_files": False, "participants": ["U1"],
        }) + "\n"
    )

    env = Environment(
        loader=FileSystemLoader(str(Path(render.__file__).parent / "templates")),
        autoescape=select_autoescape(["html"]),
    )

    html_root = tmp_path / "html"
    html_root.mkdir()
    (html_root / "channels").mkdir()

    render.render_channel_html(cdir, html_root, {}, {}, env, "2026-05-20 TEST")

    t1_html = html_root / "channels" / "C001" / "threads" / "T1.html"
    chan_index = html_root / "channels" / "C001" / "index.html"

    assert t1_html.exists(), "T1 (valid) should render even though T2 fails"
    assert chan_index.exists(), "Channel index should still be written"

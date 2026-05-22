#!/usr/bin/env python3
"""
v0.7 web service: full-text search on top of the static HTML viewer.

  /                   → static html/ tree (existing channel + thread pages)
  /search?q=...       → HTML search page rendering hits with click-through links
  /api/search?q=...   → JSON hits — same data, machine-readable
  /healthz            → liveness

The server is intentionally thin: indexer.search() does the FTS5 work; the
server adds the URL-construction + HTML rendering, and re-uses _base.html
so the search page inherits the existing CSS look.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from slack_log import indexer
from slack_log.sync import SyncManager, scheduler_loop

# Fixed UTC+8 offset for grouping user-timeline messages onto a consistent
# calendar day. A fixed offset avoids a tzdata dependency in the slim image;
# per-viewer wall-clock times are rendered browser-side from raw epochs.
_TIMELINE_TZ = timezone(timedelta(hours=8))


def _hit_url(hit: dict) -> str:
    return f"/channels/{hit['channel_id']}/threads/{hit['thread_ts']}#msg-{hit['ts']}"


def _enrich(hit: dict) -> dict:
    hit = dict(hit)
    hit["url"] = _hit_url(hit)
    return hit


def _display_name(u: dict) -> str:
    return u.get("display_name") or u.get("real_name") or u.get("name") or ""


def _avatar(u: dict) -> str | None:
    return u.get("image_72") or u.get("image_48") or u.get("image_192") or u.get("image_24")


def _human_time(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), _TIMELINE_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ts


def create_app(
    db_path: Path,
    html_root: Path,
    data_root: Path | None = None,
    templates_root: Path | None = None,
    include: set[str] | None = None,
    sync_token: str | None = None,
    sync_interval: float = 0,
    sync_script: Path | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a search.db + html/ tree.

    data_root is needed for the /user/{uid} page (looks up avatar + display
    name from users.json). If absent, /user endpoints 404.

    include: subset of {channel, dm, mpim}. None = all. Applied at query time
    so the same search.db can serve different views without rebuilding.
    """
    db_path = Path(db_path)
    html_root = Path(html_root)
    data_root = Path(data_root) if data_root else None
    include = set(include) if include else None
    if templates_root is None:
        templates_root = Path(__file__).parent / "templates"
    # Two flavors live side-by-side: static/ (relative .html for `python -m http.server`)
    # and server/ (no-suffix absolute URLs for this FastAPI server). Pick server/.
    if (templates_root / "server").is_dir():
        templates_root = templates_root / "server"
    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=select_autoescape(["html"]),
    )

    users: dict = {}
    if data_root and (data_root / "users.json").exists():
        users = json.loads((data_root / "users.json").read_text())

    def _fetched_at() -> str:
        """slackdump's last archive time, as a unix epoch string. The browser
        renders it in the visitor's local timezone (see the localtime JS)."""
        for cand in (
            Path("raw/slackdump.sqlite"),
            (data_root or Path("data")) / "users.json",
        ):
            try:
                if cand.exists():
                    return str(cand.stat().st_mtime)
            except OSError:
                pass
        return ""

    def _now() -> str:
        """Current time as a unix epoch string — browser renders it local."""
        return str(datetime.now().timestamp())

    def _kind_clause() -> tuple[str, list]:
        if not include:
            return "", []
        placeholders = ",".join("?" * len(include))
        return f" AND kind IN ({placeholders})", sorted(include)

    # Sync manager — serialises the refresh pipeline; the background
    # scheduler and POST /sync both go through it (see sync.py).
    if sync_script is None:
        sync_script = Path(__file__).parent.parent / "scripts" / "refresh.sh"
    sync_manager = SyncManager(script=sync_script, cwd=sync_script.parent.parent)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        scheduler: asyncio.Task | None = None
        if sync_interval > 0:
            scheduler = asyncio.create_task(scheduler_loop(sync_manager, sync_interval))
        try:
            yield
        finally:
            if scheduler:
                scheduler.cancel()

    app = FastAPI(title="slack-log search", version="0.7.0", lifespan=_lifespan)

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException):
        if exc.status_code != 404:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        # Static-file 404s also flow through this handler — render the friendly page.
        # JSON path stays for /api/* so machine callers still get structured errors.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=404)
        body = env.get_template("not_found.html").render(
            detail=exc.detail,
            fetched_at=_fetched_at(), generated_at=_now(),
        )
        return HTMLResponse(body, status_code=404)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    def _check_sync_token(request: Request) -> None:
        if not sync_token:
            raise HTTPException(status_code=503, detail="sync API not configured")
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {sync_token}"):
            raise HTTPException(status_code=401, detail="invalid or missing sync token")

    @app.post("/sync")
    async def trigger_sync(request: Request):
        """Trigger an immediate refresh. 202 if started, 409 if one is running."""
        _check_sync_token(request)
        started = await sync_manager.trigger("api")
        if not started:
            raise HTTPException(status_code=409, detail="a sync is already running")
        return JSONResponse({"status": "started"}, status_code=202)

    @app.get("/sync")
    async def sync_status(request: Request):
        """Current sync state — running flag, last run time and result."""
        _check_sync_token(request)
        return JSONResponse(sync_manager.status())

    @app.get("/api/search")
    def api_search(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=500)):
        q = q.strip()
        if not q:
            return JSONResponse({"query": "", "total": 0, "hits": []})
        with sqlite3.connect(db_path) as conn:
            hits = [_enrich(h) for h in indexer.search(conn, q, limit=limit, include=include)]
        return JSONResponse({"query": q, "total": len(hits), "hits": hits})

    @app.get("/search", response_class=HTMLResponse)
    def search_page(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=500)):
        q = q.strip()
        hits: list[dict] = []
        if q:
            with sqlite3.connect(db_path) as conn:
                hits = [_enrich(h) for h in indexer.search(conn, q, limit=limit, include=include)]
        tmpl = env.get_template("search.html")
        return HTMLResponse(tmpl.render(
            query=q, hits=hits, total=len(hits),
            fetched_at=_fetched_at(), generated_at=_now(),
        ))

    def _user_messages(uid: str, limit: int) -> list[dict]:
        """All messages by uid, newest first, with deep-link url."""
        kind_clause, kind_params = _kind_clause()
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                f"SELECT ts, thread_ts, channel_id, channel_name, user_name, text "
                f"FROM messages WHERE user_id = ?{kind_clause} "
                f"ORDER BY ts DESC LIMIT ?",
                (uid, *kind_params, limit),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["text"] = indexer.join_cjk(r["text"])
            r["url"] = _hit_url(r)
            r["human_time"] = _human_time(r["ts"])
            r["time_short"] = r["human_time"][11:]  # HH:MM:SS
            r["date"] = r["human_time"][:10]        # YYYY-MM-DD
        return rows

    def _group_days(msgs: list[dict]) -> list[dict]:
        """Adjacent same-date messages collapse into one day group."""
        days: list[dict] = []
        for m in msgs:
            if not days or days[-1]["date"] != m["date"]:
                days.append({"date": m["date"], "msgs": []})
            days[-1]["msgs"].append(m)
        return days

    def _timeline_segments(messages: list[dict]) -> list[dict]:
        """Adjacent same-channel messages collapse into one segment; days grouped inside.
          [{channel_name, channel_id, days:[{date, msgs:[...]}]}, ...]
        """
        segments: list[list[dict]] = []  # each item: list of msgs for one channel run
        meta: list[dict] = []
        for m in messages:
            if not segments or meta[-1]["channel_name"] != m["channel_name"]:
                segments.append([])
                meta.append({"channel_name": m["channel_name"], "channel_id": m["channel_id"]})
            segments[-1].append(m)
        return [
            {**meta[i], "days": _group_days(segments[i])}
            for i in range(len(segments))
        ]

    def _by_channel_segments(messages: list[dict]) -> list[dict]:
        """Group all messages of a channel into one segment (regardless of adjacency).
        Each segment's days are date-grouped. Same shape as timeline segments.
        """
        by_cid: dict[str, dict] = {}
        for m in messages:
            seg = by_cid.setdefault(m["channel_id"], {
                "channel_name": m["channel_name"],
                "channel_id": m["channel_id"],
                "msgs": [],
            })
            seg["msgs"].append(m)
        out = []
        for seg in by_cid.values():
            out.append({
                "channel_name": seg["channel_name"],
                "channel_id": seg["channel_id"],
                "days": _group_days(seg["msgs"]),
            })
        return out

    @app.get("/api/user/{uid}")
    def api_user(uid: str, limit: int = Query(default=500, ge=1, le=5000)):
        u = users.get(uid)
        if not u:
            raise HTTPException(status_code=404, detail=f"unknown user {uid}")
        messages = _user_messages(uid, limit)
        return JSONResponse({
            "user_id": uid,
            "user_name": _display_name(u),
            "avatar": _avatar(u),
            "total": len(messages),
            "messages": messages,
            "segments": _timeline_segments(messages),
            "by_channel_segments": _by_channel_segments(messages),
        })

    @app.get("/user/{uid}", response_class=HTMLResponse)
    def user_page(
        uid: str,
        view: str = Query(default="timeline", pattern="^(timeline|by_channel)$"),
        limit: int = Query(default=500, ge=1, le=5000),
    ):
        u = users.get(uid)
        if not u:
            raise HTTPException(status_code=404, detail=f"unknown user {uid}")
        messages = _user_messages(uid, limit)
        tmpl = env.get_template("user.html")
        return HTMLResponse(tmpl.render(
            user_id=uid,
            user_name=_display_name(u),
            avatar=_avatar(u),
            total=len(messages),
            messages=messages,
            segments=_timeline_segments(messages),
            by_channel_segments=_by_channel_segments(messages),
            view=view,
            fetched_at=_fetched_at(), generated_at=_now(),
        ))

    # Pages are rendered dynamically from the jsonl data layer — no pre-built
    # html/ tree. Template edits take effect immediately; the refresh pipeline
    # only needs archive → split → attach → index, no render step.
    from fastapi.responses import FileResponse
    from slack_log import render as R

    channels_meta: dict = {}
    if data_root and (data_root / "channels.json").exists():
        channels_meta = json.loads((data_root / "channels.json").read_text())

    def _channel_name(cid: str) -> str:
        return (channels_meta.get(cid) or {}).get("name") or cid

    @app.get("/", response_class=HTMLResponse)
    def home():
        groups = R.build_global_groups(data_root or Path("data"), channels_meta, users, include=include)
        tmpl = env.get_template("global_index.html")
        return HTMLResponse(tmpl.render(
            **groups, fetched_at=_fetched_at(), generated_at=_now()))

    @app.get("/channels/{cid}", response_class=HTMLResponse)
    def channel_index(cid: str):
        cdir = (data_root or Path("data")) / "channels" / cid
        if not cdir.is_dir():
            raise HTTPException(status_code=404, detail=f"channel {cid} not found")
        threads_meta = R.enrich_thread_meta(R.load_thread_meta(cdir), users, channels_meta)
        tmpl = env.get_template("channel_index.html")
        return HTMLResponse(tmpl.render(
            channel_id=cid, channel_name=_channel_name(cid), threads=threads_meta,
            fetched_at=_fetched_at(), generated_at=_now()))

    @app.get("/channels/{cid}/threads/{ts}", response_class=HTMLResponse)
    def thread_page(cid: str, ts: str):
        cdir = (data_root or Path("data")) / "channels" / cid
        ttp = cdir / "threads" / f"{ts}.jsonl"
        if not ttp.exists():
            raise HTTPException(status_code=404, detail=f"thread {ts} not found")
        msgs = R.enrich_messages(R.load_thread(ttp), users, channels_meta, cdir)
        tm = next((t for t in R.load_thread_meta(cdir) if t.get("thread_ts") == ts), None)
        if tm is None:
            tm = {"thread_ts": ts, "is_thread": len(msgs) > 1,
                  "reply_count": max(0, len(msgs) - 1)}
        tmpl = env.get_template("thread.html")
        return HTMLResponse(tmpl.render(
            channel_id=cid, channel_name=_channel_name(cid), thread_meta=tm,
            messages=msgs, fetched_at=_fetched_at(), generated_at=_now()))

    @app.get("/channels/{cid}/attachments/{fname}")
    def attachment(cid: str, fname: str):
        p = (data_root or Path("data")) / "channels" / cid / "attachments" / fname
        if not p.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(p)

    # Optional OIDC auth — activates only when OIDC_* env vars are present
    # (production). Dev and pytest run unauthenticated.
    from slack_log.auth import auth_config_from_env, install_auth

    cfg = auth_config_from_env()
    if cfg:
        install_auth(app, **cfg)

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn --factory entrypoint for the container.

    Reads paths from env; in EKS a single PVC is mounted and SLACK_LOG_ROOT
    points at it, so search.db / html/ / data/ all live on the shared volume.
    """
    root = Path(os.environ.get("SLACK_LOG_ROOT", "."))
    inc = {p.strip() for p in os.environ.get("SLACK_LOG_INCLUDE", "").split(",") if p.strip()}
    return create_app(
        db_path=Path(os.environ.get("SLACK_LOG_DB", root / "search.db")),
        html_root=Path(os.environ.get("SLACK_LOG_HTML", root / "html")),
        data_root=Path(os.environ.get("SLACK_LOG_DATA", root / "data")),
        include=inc or None,
        sync_token=os.environ.get("SLACK_LOG_SYNC_TOKEN") or None,
        sync_interval=float(os.environ.get("SLACK_LOG_SYNC_INTERVAL", "0") or "0"),
    )


def main():
    ap = argparse.ArgumentParser(description="Run the slack-log search web service")
    ap.add_argument("--db", type=Path, default=Path("./search.db"))
    ap.add_argument("--html", type=Path, default=Path("./html"))
    ap.add_argument("--data", type=Path, default=Path("./data"),
                    help="data/ root — needed for /user/<uid> avatar + name lookup")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--include", default="",
                    help="comma-separated subset of channel/dm/mpim (default: all)")
    args = ap.parse_args()

    include = {p.strip() for p in args.include.split(",") if p.strip()} or None
    import uvicorn
    app = create_app(db_path=args.db, html_root=args.html, data_root=args.data, include=include)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

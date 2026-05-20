<sub><b>🌐 English</b> · <a href="README.zh.md">中文</a></sub>

# slack-log

Turn a Slack workspace into a **static IRC-log-style HTML viewer** with permanent ref-id anchors,
plus a **machine-friendly JSONL data layer** that AI agents and shell `grep` can read directly.

### Why I built this

I keep needing to grep through old Slack threads — onboarding people, writing weekly reports,
hunting down decisions from three months ago. Slack's own search caps at 10k messages on free
plans, and even on paid plans I can't pipe a thread into `grep` or feed it to an AI.

Existing tools all stop halfway. [slackdump](https://github.com/rusq/slackdump) nails the hard
part (auth, rate-limit handling, incremental resume) but outputs SQLite + per-day JSON, not
"one file per thread." [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) is a
Flask server that needs to keep running. Neither emits stable per-message anchors I can paste
into a doc and trust forever.

So slack-log sits **on top of slackdump**, doing only the things slackdump doesn't:

1. Split the SQLite into one JSONL per thread, named by `thread_ts` (Slack's stable unique id).
2. Render static HTML with `<a id="msg-{ts}">` anchors — paste a URL into any document and it
   will still point at the same message a year from now.
3. Differentiate-download attachments by mime/size (images yes, large zips just metadata).
4. Resolve every uid/cid to display name, render mrkdwn / unfurl cards / reactions popup /
   lightbox so the result looks close to native Slack.

### Why you might want it

- **One file per thread, pure JSONL.** Each thread is `data/channels/<cid>/threads/<thread_ts>.jsonl`.
  Full Slack API fields preserved (blocks, reactions, files, edited, attachments) — every line is
  a complete message event. `grep`, `jq`, AI prompts all work directly.
- **Permanent ref ids.** Every message becomes `<a id="msg-{ts}">`. URL like
  `…/threads/1779079280.797169.html#msg-1779154899.648009` is a stable citation you can drop into
  any doc.
- **No backend.** `python3 render.py` emits static HTML you open with `file://` or any static
  host. Lightbox, sort tabs, reaction popups all run on vanilla JS (~60 lines, zero dependencies).
- **Standing on slackdump's shoulders.** Auth, rate limits, incremental resume, thread-reply
  late-arrival detection — slackdump handles all of it. slack-log invokes it as a subprocess.
- **Dual format.** JSONL for AI / shell, SQLite (slackdump's) kept untouched for SQL / future
  backend.
- **Selective rendering.** `make render-channels` to skip DMs/MPIMs, useful for sharing channel
  archives without leaking private chats.

> Personal project, MIT-licensed. Tested on a single Slack workspace on macOS. Public channels,
> private channels, DMs, MPIMs all work. Bot accounts not tested.

## Quick start

```sh
# 1. Install dependencies
brew install slackdump
pip install pyyaml jinja2 emoji

# 2. Hand slackdump your Slack credentials (xoxc + xoxd from browser cookies)
#    See https://github.com/rusq/slackdump/wiki/EZ-Login-3000 for the easy way.
slackdump workspace new

# 3. Pull the workspace into raw/slackdump.sqlite
make fetch

# 4. Build everything (split SQLite → JSONL, download attachments, render HTML)
make update

# 5. Open the static site
cd html && python3 -m http.server 8765
open http://localhost:8765/
```

### Slack credentials for attach

`slackdump` handles its own auth, but `attach.py` also needs your Slack
browser token (`xoxc-...`) and cookie (`xoxd-...`) to download private file
attachments. Resolution order:

1. `SLACK_XOXC` + `SLACK_XOXD` environment variables
2. `./.env` in the current working directory (project-local)
3. `~/.config/slack-log/.env` (per-user, XDG-respecting)

```sh
# Option A: env vars
export SLACK_XOXC=xoxc-your-token
export SLACK_XOXD=xoxd-your-cookie

# Option B: per-user .env (chmod 600!)
mkdir -p ~/.config/slack-log
cat > ~/.config/slack-log/.env <<EOF
SLACK_XOXC=xoxc-your-token
SLACK_XOXD=xoxd-your-cookie
EOF
chmod 600 ~/.config/slack-log/.env
```

Files use the standard `.env` format parsed by [python-dotenv]. Same values
that `slackdump workspace import` accepted — see
<https://github.com/rusq/slackdump/wiki/EZ-Login-3000> for how to read them
from a browser session.

[python-dotenv]: https://github.com/theskumar/python-dotenv

## Common workflow

```sh
make update              # full incremental: fetch → split → attach → render
make fetch               # slackdump archive --resume only (cheap, additive)
make reconcile           # re-fetch last 90 days to pick up edits/deletes (weekly)
make rebuild-html        # template/CSS changes only — keeps data/, fastest path
make render-channels     # render only public/private channels (skip DMs/MPIMs)
make render-dms          # only DMs
make help                # list all targets
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Consumers: weekly reports / AI runbook / search │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  View + skill layer                              │
│  HTML (jinja2 + ref id + sort + lightbox)        │
│  Claude skill (reads JSONL — no Slack API call)  │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  Data layer (dual format)                        │
│  thread JSONL  ← AI / grep                       │
│  channel index.jsonl + users.json + channels.json│
│  slackdump.sqlite ← SQL / future backend         │
│  attachments/  (differentiated download)         │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  Collection layer (slackdump subprocess)         │
│  archive + Lookback resume + rate limit + auth   │
└──────────────────────────────────────────────────┘
```

slack-log itself is **~900 lines of Python + Jinja2**. The collection layer is delegated entirely
to slackdump (AGPLv3, runs as a child process — does not affect slack-log's MIT license).

## Files

| Path | Notes |
|---|---|
| `slack_log/splitter.py` | `slackdump.sqlite` → per-thread JSONL + `users.json` + `channels.json` |
| `slack_log/attach.py` | Walks JSONL, downloads attachments by mime/size policy, writes `.meta.json` for every file (downloaded or not) |
| `slack_log/render.py` | JSONL → static HTML. Resolves mentions, parses mrkdwn, builds reaction popups, unfurl cards |
| `slack_log/templates/` | `_base.html` (CSS + lightbox JS) + `global_index` / `channel_index` / `thread.html` |
| `tests/` | pytest suite (error-recovery tests for splitter / attach / render) |
| `pyproject.toml` | Package metadata, dependencies, console scripts, pytest config |
| `Makefile` | Build targets (`update`, `fetch`, `reconcile`, `rebuild-html`, `render-channels`, `test`…) |

## Design notes

- **Attachments survive HTML rebuilds.** `data/` is precious (slow re-download), `html/` is
  cheap. `make rebuild-html` only touches `html/`. Only `make clean-all` removes both.
- **Stable file names.** Threads are named by `thread_ts` (Slack's unique id) — not by
  date or preview text, so URLs never change.
- **`.meta.json` for every file, downloaded or not.** Big zips and videos get only metadata; the
  original Slack URL is preserved so you can fetch them later.
- **HTML rendering is HTML-aware about `<a>` nesting.** Slack mrkdwn URLs inside channel-index
  preview snippets are downgraded to `<span>` to avoid the HTML spec's "no nested `<a>`" rule
  silently breaking layout.
- **Edits and deletes are reconciled by re-fetch, not by event.** Slack does not push
  `message_changed` / `message_deleted` over the REST archive path. `make reconcile` re-fetches
  the last `RECONCILE_DAYS` (default 90) into a new session; splitter dedupes by
  `MAX(LOAD_DTTM)` so the latest version of every message wins. Run weekly.

## Roadmap

- [x] v0.1 splitter MVP
- [x] v0.2 full workspace archive
- [x] v0.3 static HTML with ref ids and sort tabs
- [x] v0.4 fine-grained rendering (mentions / mrkdwn / unfurls / reactions popup / lightbox / fallbacks)
- [x] v0.5 edit/delete reconciliation via `make reconcile` (90-day re-fetch + dedup by LOAD_DTTM)
- [x] v0.6 progress bars (tqdm) + per-element error recovery + pytest suite + package layout
- [ ] v0.7 web service — HTTP browsing + full-text search, designed for server deployment

## Acknowledgements

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov — the collection layer that
  makes slack-log possible. AGPLv3.
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) — UI inspiration for the
  Jinja2 template starting point.

## License

[MIT](LICENSE) © Xie Yanbo, 2026.

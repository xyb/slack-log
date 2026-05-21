<sub><b>🌐 English</b> · <a href="README.zh.md">中文</a></sub>

# slack-log

[![CI](https://github.com/xyb/slack-log/actions/workflows/ci.yml/badge.svg)](https://github.com/xyb/slack-log/actions/workflows/ci.yml)

Turn a Slack workspace into a **searchable web service** with permanent ref-id
anchors — dynamic pages, full-text search, optional OIDC SSO — backed by a
**machine-friendly JSONL data layer** that AI agents and shell `grep` read
directly. Can also export plain static HTML for no-backend hosting.

### Why I built this

I keep needing to grep through old Slack threads — onboarding people, writing
weekly reports, hunting down decisions from three months ago. Slack's own
search caps at 10k messages on free plans, and even on paid plans I can't pipe
a thread into `grep` or feed it to an AI.

Existing tools all stop halfway. [slackdump](https://github.com/rusq/slackdump)
nails the hard part (auth, rate-limit handling, incremental resume) but outputs
SQLite + per-day JSON, not "one file per thread."
[slack-export-viewer](https://github.com/hfaran/slack-export-viewer) is a Flask
server with no auth and no stable per-message anchors.

So slack-log sits **on top of slackdump**, doing only the things slackdump
doesn't:

1. Split the SQLite into one JSONL per thread, named by `thread_ts` (Slack's
   stable unique id).
2. Serve dynamic pages — or render static HTML — with `<a id="msg-{ts}">`
   anchors, so a URL pasted into a document still points at the same message a
   year later.
3. Full-text search (SQLite FTS5), per-user timelines, dark/light themes,
   English/Chinese UI.
4. Differentiated attachment download by mime/size (images yes, large zips
   metadata-only).
5. Resolve every uid/cid to display name, render mrkdwn / unfurl cards /
   reactions / lightbox so the result looks close to native Slack.

### What you get

- **A real web service.** A FastAPI server renders every page dynamically from
  the JSONL layer — channel lists, threads, per-user timelines, attachments —
  with FTS5 full-text search. Dark/light themes, English/Chinese UI, timestamps
  rendered in each visitor's own browser timezone. Optional OIDC SSO makes it
  safe to deploy behind a login.
- **…or zero backend.** `make render-static` emits plain static HTML you open
  with `file://` or drop on any static host — same pages, relative links.
- **One file per thread, pure JSONL.** Each thread is
  `data/channels/<cid>/threads/<thread_ts>.jsonl`. Full Slack API fields
  preserved (blocks, reactions, files, edited, attachments) — every line is a
  complete message event. `grep`, `jq`, AI prompts all work directly.
- **Permanent ref ids.** Every message becomes `<a id="msg-{ts}">`. A URL like
  `…/threads/1779079280.797169#msg-1779154899.648009` is a stable citation you
  can drop into any doc.
- **Standing on slackdump's shoulders.** Auth, rate limits, incremental resume,
  thread-reply late-arrival detection — slackdump handles all of it. slack-log
  invokes it as a subprocess.
- **Container-ready.** A public multi-arch Docker image runs the server; a
  daily CronJob re-archives. Kubernetes manifests included.

> Personal project, MIT-licensed. Tested on a single Slack workspace. Public
> channels, private channels, DMs, MPIMs all work.

## Quick start

### Run the web service locally

```sh
brew install slackdump
pip install -e .

# Give slackdump your Slack credentials (xoxc + xoxd from browser cookies).
# Easy way: https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new

make fetch && make split && make attach && make index
make serve            # → http://127.0.0.1:8770
```

### …or export static HTML

```sh
make render-static    # → html-static/, open with file:// or any static host
```

### …or run the container

```sh
docker run -p 8770:8770 -v "$PWD/data:/data" xieyanbo/slack-log:0.9.0
```

### Slack credentials for attach

`slackdump` handles its own auth, but `attach.py` also needs your Slack browser
token (`xoxc-...`) and cookie (`xoxd-...`) to download private file
attachments. Resolution order:

1. `SLACK_XOXC` + `SLACK_XOXD` environment variables
2. `./.env` in the current working directory (project-local)
3. `~/.config/slack-log/.env` (per-user, XDG-respecting)

```sh
# per-user .env (chmod 600!)
mkdir -p ~/.config/slack-log
cat > ~/.config/slack-log/.env <<EOF
SLACK_XOXC=xoxc-your-token
SLACK_XOXD=xoxd-your-cookie
EOF
chmod 600 ~/.config/slack-log/.env
```

Files use the standard `.env` format parsed by
[python-dotenv](https://github.com/theskumar/python-dotenv).

## Deployment

slack-log ships as a public Docker image and a set of Kubernetes manifests:

- **Image** — `xieyanbo/slack-log` on Docker Hub, multi-arch (amd64/arm64).
  Each release publishes a pinned `X.Y.Z` tag; `:latest` tracks the highest
  released version. Deployments pin the exact version.
- **Kubernetes** — `deploy/k8s/` holds sanitized `*.example.yaml` manifests:
  Deployment + Service + Ingress + a shared PVC + a daily refresh CronJob. Copy
  an example to the same name without `.example`, fill in your real values, and
  apply that — the real copy stays untracked.
- **OIDC SSO** — set `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` /
  `OIDC_DISCOVERY_URL` and the server requires login; leave them unset and it
  runs open, for local dev. `/healthz` is always public.
- **CI/CD** — see [docs/CICD.md](docs/CICD.md). Every push/PR runs the test
  matrix + lint; pushing a `vX.Y.Z` tag builds and publishes the image
  automatically.

## Common workflow

```sh
make fetch               # slackdump archive --resume only (cheap, additive)
make split               # SQLite → per-thread JSONL + users/channels
make attach              # download attachments by mime/size policy
make index               # build search.db (FTS5 full-text index)
make serve               # run the web service on 127.0.0.1:8770
make render-static       # export the static-HTML flavor
make reconcile           # re-fetch last 90 days to pick up edits/deletes (weekly)
make help                # list all targets
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Consumers: weekly reports / AI runbook / search │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  Serving layer                                   │
│  FastAPI server — dynamic pages + FTS5 search    │
│    + OIDC SSO + dark/light + i18n                │
│  Static HTML export (render.py, no backend)      │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  Data layer (dual format)                        │
│  thread JSONL  ← AI / grep / server              │
│  channel index.jsonl + users.json + channels.json│
│  search.db (FTS5)                                │
│  slackdump.sqlite ← SQL / archive of record      │
│  attachments/  (differentiated download)         │
└──────────────────────────────────────────────────┘
                       ↑
┌──────────────────────────────────────────────────┐
│  Collection layer (slackdump subprocess)         │
│  archive + Lookback resume + rate limit + auth   │
└──────────────────────────────────────────────────┘
```

The collection layer is delegated entirely to slackdump (AGPLv3, runs as a
child process — does not affect slack-log's MIT license).

## Files

| Path | Notes |
|---|---|
| `slack_log/splitter.py` | `slackdump.sqlite` → per-thread JSONL + `users.json` + `channels.json` |
| `slack_log/attach.py` | Walks JSONL, downloads attachments by mime/size policy, writes `.meta.json` for every file |
| `slack_log/indexer.py` | Builds `search.db` — a SQLite FTS5 full-text index over the JSONL |
| `slack_log/render.py` | Shared render functions; also the static-HTML exporter |
| `slack_log/server.py` | FastAPI app — dynamic pages, search, per-user timelines, attachment serving |
| `slack_log/auth.py` | Optional OIDC SSO middleware + access logging |
| `slack_log/templates/` | `server/` (dynamic, absolute URLs) + `static/` (relative `.html`) flavors |
| `deploy/k8s/` | Sanitized Kubernetes manifests (`*.example.yaml`) |
| `docs/CICD.md` | CI/CD design + operations reference |
| `Makefile` | Build / serve targets |

## Design notes

- **Dynamic by default.** The server renders pages straight from the JSONL
  layer — no pre-built HTML tree, so template edits take effect immediately and
  the refresh pipeline needs no render step.
- **Attachments survive rebuilds.** `data/` is precious (slow re-download).
  Only `make clean-all` removes it.
- **Stable file names.** Threads are named by `thread_ts` (Slack's unique id),
  not by date or preview text, so URLs never change.
- **`.meta.json` for every file, downloaded or not.** Big zips and videos get
  only metadata; the original Slack URL is preserved so you can fetch later.
- **Edits and deletes are reconciled by re-fetch, not by event.** Slack does
  not push `message_changed` / `message_deleted` over the REST archive path.
  `make reconcile` re-fetches the last 90 days; splitter dedupes by
  `MAX(LOAD_DTTM)` so the latest version of every message wins. Run weekly.

## Roadmap

- [x] v0.1–v0.6 — splitter, full-workspace archive, static HTML with ref ids,
  fine-grained rendering, edit/delete reconciliation, error recovery + tests
- [x] v0.7 — web service: HTTP browsing + FTS5 full-text search + per-user
  timelines + dark/light + English/Chinese i18n
- [x] v0.8 — OIDC SSO, Docker image, Kubernetes manifests
- [x] v0.9 — fully dynamic rendering, browser-local timestamps, access logging,
  GitHub Actions CI/CD

## Acknowledgements

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov — the
  collection layer that makes slack-log possible. AGPLv3.
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) — UI
  inspiration for the Jinja2 template starting point.

## License

[MIT](LICENSE) © Xie Yanbo, 2026.

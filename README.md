<sub><b>🌐 English</b> · <a href="README.zh.md">中文</a></sub>

# slack-log

[![CI](https://github.com/xyb/slack-log/actions/workflows/ci.yml/badge.svg)](https://github.com/xyb/slack-log/actions/workflows/ci.yml)

Turn a Slack workspace into something you can actually read, `grep`, and search
— with permanent ref-id anchors so a link to a message still works a year
later. One codebase, **two product shapes**: run it on your laptop, or run it
as a team web service.

### Why I built this

I keep needing to grep through old Slack threads — onboarding people, writing
weekly reports, hunting down decisions from three months ago. Slack's own
search caps at 10k messages on free plans, and even on paid plans I can't pipe
a thread into `grep` or feed it to an AI.

slack-log sits **on top of [slackdump](https://github.com/rusq/slackdump)**,
which nails the hard part (auth, rate-limit handling, incremental resume). It
adds the things slackdump doesn't: a one-file-per-thread JSONL layer, a web
service with stable per-message anchors, full-text search, and rendering that
looks close to native Slack.

## Choose your profile

slack-log ships as one codebase with two product shapes. Pick the one that
matches how you'll use it — a `SLACK_LOG_PROFILE` switch is the only difference.

|                | **Personal**                              | **Team**                                    |
|----------------|-------------------------------------------|---------------------------------------------|
| Runs on        | your laptop                               | a server                                    |
| Core artifact  | the `data/` JSONL layer                   | `search.db` (one SQLite file)               |
| Use it for     | `grep`, feeding an AI, local browsing     | a shared, searchable web archive            |
| Web server     | local, no login                           | FastAPI + OIDC SSO                          |
| Refresh        | manual (`make personal-build`)            | built-in scheduler + `POST /sync`           |
| Deploy         | —                                         | Docker image + Kubernetes manifests         |
| Guide          | [docs/personal.md](docs/personal.md)      | [docs/team.md](docs/team.md)                |

Both profiles share the same collection layer (slackdump), the same FTS5
search, and the same rendering — see [docs/architecture.md](docs/architecture.md).

## Personal profile

A local archive of your own Slack. The splitter writes a **machine-friendly
JSONL data layer** — one file per thread — that `grep`, `jq` and AI prompts
read directly. A local web server browses it; a static-HTML export needs no
backend at all.

```sh
brew install slackdump
pip install -e .

# Give slackdump your Slack credentials (xoxc + xoxd from browser cookies).
# Easy way: https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new

make personal-build      # slackdump archive → split → attach → index
make personal-serve      # → http://127.0.0.1:8770
```

Each thread is `data/channels/<cid>/threads/<thread_ts>.jsonl` — full Slack
fields preserved (blocks, reactions, files, edited), one complete message per
line. Or skip the server entirely:

```sh
make render-static       # → html-static/, open with file:// or any static host
```

Full walkthrough — the data layer, attachments, edit/delete reconciliation —
in [docs/personal.md](docs/personal.md).

## Team profile

A shared web archive for a team. No JSONL layer: the indexer ETLs slackdump's
archive straight into `search.db`, and the server reads that one file. FastAPI,
OIDC SSO, an in-process refresh scheduler, and a container image.

```sh
docker run -p 8770:8770 \
  -e SLACK_LOG_PROFILE=team \
  -v "$PWD/data:/data" \
  xieyanbo/slack-log:0.10.0
```

For a real deployment, `deploy/k8s/` holds sanitized Kubernetes manifests
(Deployment + Service + Ingress + a shared PVC) and the server refreshes
itself — a background scheduler plus an on-demand `POST /sync`. OIDC SSO turns
on as soon as the `OIDC_*` env vars are set.

Full deployment guide — the `search.db` schema, SSO, refresh, configmap — in
[docs/team.md](docs/team.md).

## What both profiles give you

- **Permanent ref ids.** Every message renders as `<a id="msg-{ts}">`. A URL
  like `…/threads/1779079280.797169#msg-1779154899.648009` is a stable
  citation you can drop into any document.
- **Full-text search.** SQLite FTS5, with CJK handled char-by-char so a
  two-character Chinese word still matches. Per-user timelines too.
- **Native-ish rendering.** uid/cid resolved to display names, mrkdwn, link
  unfurl cards, reactions, image lightbox — dark/light themes, English/Chinese
  UI, timestamps in each visitor's own timezone.
- **Standing on slackdump's shoulders.** Auth, rate limits, incremental
  resume, late thread-reply detection — all handled by slackdump, invoked as
  a subprocess (AGPLv3, does not affect slack-log's MIT license).

> Personal project, MIT-licensed. Tested on a single Slack workspace. Public
> channels, private channels, DMs and MPIMs all work.

## Architecture

```
        slackdump archive  ─────────────►  raw/slackdump.sqlite
                                                   │
              ┌────────────────────────────────────┴───────────────┐
        personal                                                  team
              │                                                     │
        splitter → data/ jsonl                          indexer ETL ─┘
              │            │                                     │
        attach (files)   indexer                                 ▼
              │            │                                  search.db
              ▼            ▼                          (messages + message_raw
        data/ + search.db                              + threads + channels
              │                                              + users)
              ▼                                                  │
        JsonlStore ─────────►  ArchiveStore  ◄───────── SqliteStore
                                     │
                                FastAPI server
```

The server depends only on `ArchiveStore`; `JsonlStore` and `SqliteStore` are
the two backends. Design details — the store abstraction, the `core/` shared
layer, the extended `search.db` schema — in
[docs/architecture.md](docs/architecture.md).

## Files

| Path | Notes |
|---|---|
| `slack_log/core/` | shared layer — `slackdump_db` (read the archive SQLite) + `text` (Slack text processing) |
| `slack_log/store/` | `ArchiveStore` abstraction + `JsonlStore` (personal) / `SqliteStore` (team) |
| `slack_log/config.py` | `Profile` enum + `Config.from_env` |
| `slack_log/pipeline/` | data processing — `split` · `attach` · `index` |
| `slack_log/web/` | serving — `app` (FastAPI) · `presenter` · `static_export` · `auth` · `sync` |
| `deploy/k8s/` | sanitized Kubernetes manifests (`*.example.yaml`) |
| `docs/` | `personal.md` · `team.md` · `architecture.md` · `CICD.md` |

## Roadmap

- [x] v0.1–v0.6 — splitter, full-workspace archive, static HTML with ref ids,
  fine-grained rendering, edit/delete reconciliation, error recovery + tests
- [x] v0.7 — web service: HTTP browsing + FTS5 search + per-user timelines
- [x] v0.8 — OIDC SSO, Docker image, Kubernetes manifests
- [x] v0.9 — fully dynamic rendering, browser-local timestamps, CI/CD
- [x] v0.10 — in-process refresh, splitter rewrite (N+1 → three linear passes)
- [x] v0.11 — personal / team profile split: the `ArchiveStore` abstraction,
  two backends, one `SLACK_LOG_PROFILE` switch

## Acknowledgements

- [slackdump](https://github.com/rusq/slackdump) by Rustam Useldinov — the
  collection layer that makes slack-log possible. AGPLv3.
- [slack-export-viewer](https://github.com/hfaran/slack-export-viewer) — UI
  inspiration for the Jinja2 template starting point.

## License

[MIT](LICENSE) © Xie Yanbo, 2026.

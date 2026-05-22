# Architecture

slack-log is one codebase that ships as two product shapes — a personal local
tool and a team web service. This document explains how the split works.

## One switch

`SLACK_LOG_PROFILE` (`personal` | `team`) is the only thing that differs
between the two shapes. It selects:

- which **data source** the index is built from,
- whether the **jsonl layer** is written,
- which **store** the web server reads through.

Everything else — the collection layer, FTS5 search, rendering — is shared.

## Package layout

```
slack_log/
  core/        shared, storage-agnostic
    slackdump_db.py   read slackdump's archive SQLite (dedup, users,
                      channels, bot identities)
    text.py           Slack text processing (mentions, links, mrkdwn,
                      emoji, CJK split/join)
  store/       the storage abstraction
    base.py           ArchiveStore (ABC) + the shared FTS5 search
    jsonl_store.py    JsonlStore  — personal backend
    sqlite_store.py   SqliteStore — team backend
  config.py    Profile enum + Config.from_env
  splitter.py  slackdump.sqlite → data/ jsonl (personal)
  attach.py    download attachments by mime/size policy
  indexer.py   build search.db — FTS index + the team ETL
  render.py    shared render helpers + the static-HTML exporter
  server.py    FastAPI app — depends only on an ArchiveStore
  auth.py      optional OIDC SSO + access logging
  sync.py      in-process refresh manager
  templates/   server/ and static/ Jinja2 flavors
```

`core/` has no dependency on a storage backend or a profile, so both the
pipeline modules and the store implementations build on it.

## ArchiveStore — the load-bearing wall

The web server never touches a jsonl file or a SQLite table directly. It
depends on `ArchiveStore`:

```
users()            {uid: profile}
channels()         {cid: meta}
list_channels()    [cid, ...]
thread_meta(cid)   per-thread metadata rows for a channel
load_thread(cid, ts)  raw message dicts for one thread
global_groups()    {channels, dms, mpims} for the home page
attachments_dir(cid)  where a channel's attachments live
fetched_at()       archive freshness
search(q)          FTS5 full-text search        ─┐ concrete in the base —
user_messages(uid) all of one user's messages   ─┘ search.db is profile-agnostic
```

`search()` and `user_messages()` are concrete in the base class: the FTS5
`messages` table exists in both profiles, so search works the same either way.
The page-data methods are abstract — each backend reads its own source.

`tests/test_store_contract.py` is the guard against drift: every property test
runs against **both** stores, and `*_stores_agree_*` compares their output
directly. The same archive must render identically whichever profile serves it.

## The two data flows

```
                    slackdump archive
                           │
                  raw/slackdump.sqlite
                           │
        ┌──────────────────┴───────────────────┐
        │ personal                        team │
        ▼                                      ▼
  splitter → data/*.jsonl            indexer ETL → search.db
  attach   → data/.../attachments         (messages + message_raw
  indexer  → search.db (FTS only)          + threads + channels + users)
        │                                      │
        ▼                                      ▼
   JsonlStore                            SqliteStore
```

**Personal.** The splitter turns the archive into one jsonl file per thread —
the core deliverable. `indexer --profile personal` builds only the FTS5
`messages` table from that jsonl. `JsonlStore` reads pages from the jsonl.

**Team.** `indexer --profile team` is an ETL: it reads slackdump.sqlite once
and fills the full `search.db` schema. No jsonl, no `data/` directory.
`SqliteStore` reads every page from `search.db`.

## search.db — slack-log's own stable schema

search.db started as an FTS-only index. The team profile serves every page
from it, so it grew into slack-log's own complete schema — decoupled from
slackdump, so a slackdump schema change can't break the web layer.

| Table         | Filled by | Purpose                                            |
|---------------|-----------|----------------------------------------------------|
| `messages`    | both      | FTS5 full-text index                               |
| `message_raw` | team      | complete message JSON, for rendering a thread page |
| `threads`     | team      | materialized per-thread metadata                   |
| `channels`    | team      | channel directory                                  |
| `users`       | team      | user directory                                     |

`open_db()` creates every table with `CREATE TABLE IF NOT EXISTS`, so an old
messages-only search.db is upgraded in place on the next open.

The `threads` table mirrors the splitter's `index.jsonl` entry field for
field — that one-to-one correspondence is what lets `SqliteStore.thread_meta`
and `JsonlStore.thread_meta` return identical rows.

This extended schema is also the intended foundation for a future data-export
API; that API is not implemented yet.

## Extending it

- **A new storage backend** (e.g. Postgres): implement `ArchiveStore`, add it
  to `tests/test_store_contract.py`'s parametrization, done.
- **A new page**: add a route to `server.py` and, if it needs new data, a
  method to `ArchiveStore` + both backends.
- **Shared logic**: if the splitter and the indexer would both need it, it
  belongs in `core/`.

# Changelog

All notable changes to **slack-log** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This file is the
human-curated highlight reel — keep entries to one tight line.

## [Unreleased]

### Added
- **CLI search — `python3 -m slack_log.search "<query>"`** (`make search Q="…"`). Chinese-aware: the query runs through the same CJK-split FTS transform the indexer uses, so a multi-char word like `黑边` matches — unlike raw `sqlite3 search.db "… MATCH '黑边'"`, which silently returns 0. Filters: `--channel`/`--user`/`--after`/`--before`/`--kind`/`--limit`, plus `--full`/`--json`. `index.search()` gained matching `channel`/`user`/`after`/`before` keyword filters (`tests/test_search_cli.py`).
- **Single locked build entry — `python3 -m slack_log.pipeline`** (`--profile personal|team`, `--max-mb`, `--include`). Orchestrates the whole build (fetch → split → attach → index) under one process-wide mutex, so concurrent builds can't corrupt `raw/slackdump.sqlite` + `search.db`.
- **`slack_log.lock.build_lock`** — non-blocking `fcntl.flock` mutex; a second build raises `BuildLockHeld` and exits immediately. The lock is tied to the fd, so the kernel auto-releases it on process exit/crash/SIGKILL (no stale lockfile to clean up).
- **Tests** — `tests/test_lock.py` (acquire/reject/release, non-blocking, pid record, auto-release after the holder is killed) and `tests/test_pipeline_orchestrator.py` (per-profile step order, lock-held → exit 1 without running the build).

### Changed
- **Concurrency lock moved out of the Makefile into the core build entry.** `make personal-build` / `team-build` are now thin delegates to `python3 -m slack_log.pipeline`. The lock sits at the one choke point every build funnels through, so it can't be bypassed by invoking a submodule directly.

## [0.11.0] - 2026-05-22

### Added
- **Team profile** — `SqliteStore` backend + a team ETL indexer that builds `search.db` straight from `slackdump.sqlite` (no jsonl layer), with per-profile config and a personal/team build & deploy split.
- **Attachments on both profiles** — `attach` downloads files for personal (jsonl) and team (search.db), with a configurable size cap (`--max-mb`).

### Changed
- **Module reorg** — code split into `pipeline/` and `web/` subpackages; a shared `core/` subpackage (slackdump_db + text) extracted; `ArchiveStore` decouples the server from file paths; `render.py` split into store reads / presenter / static_export.
- **Docs** — README split by profile + `docs/` guides.

### Fixed
- **Incremental refresh** — first run does a full archive, then resumes (delta only).
- **Two profile-drift bugs** in the store, found by real-data verification.

## [0.10.0] - 2026-05-22

### Added
- **In-process refresh** — background scheduler + `POST /sync` API; refresh output streams live instead of buffering.

### Changed
- **Splitter rewrite** — N+1 query collapsed into three linear passes.
- **CI** — `dev.yml` manual/PR-label builds with dated image tags; release moves `:latest` to the highest version.

## [0.9.0] - 2026-05-21

### Changed
- **README rewritten** for the web-service shape.
- **CI** — GitHub Actions test matrix + tag-triggered Docker release; actions bumped; ruff-action pinned.

## [0.8.11] - 2026-05-21

### Fixed
- **EKS deployment live** — dynamic render + browser-local time.

## [0.8] - 2026-05-21

### Added
- **Web service deployment** — OIDC auth, container image, Kubernetes manifests.

## [0.7] - 2026-05-20

### Added
- **Web service** — full-text search, per-user view, dark/light themes, i18n.

### Changed
- **Slack token** loaded from env vars + `.env` file (no longer from the Cursor MCP config).

## [0.6] - 2026-05-20

### Added
- **Error recovery + progress bars**, pytest suite, and a proper package layout; test coverage raised to 84%.

## [0.5] - 2026-05-20

### Added
- **Edit/delete reconciliation** via `make reconcile`.

## [0.4] - 2026-05-20

### Added
- **Initial release** — static IRC-log-style HTML viewer over a slackdump archive.

# Team profile

A shared, searchable web archive for a team. No JSONL layer: the indexer ETLs
slackdump's archive straight into `search.db`, and the server reads that one
SQLite file. FastAPI, OIDC SSO, an in-process refresh scheduler, a container.

Set `SLACK_LOG_PROFILE=team` and the whole pipeline switches over.

## Run the container

```sh
docker run -p 8770:8770 \
  -e SLACK_LOG_PROFILE=team \
  -v "$PWD/data:/data" \
  xieyanbo/slack-log:0.10.0
```

The image is on Docker Hub as `xieyanbo/slack-log`, multi-arch (amd64/arm64).
Each release publishes a pinned `X.Y.Z` tag; pin the exact version in a real
deployment. `SLACK_LOG_PROFILE=team` is already the image default — the env
var above is shown for clarity.

## Kubernetes

`deploy/k8s/` holds sanitized manifests: namespace + shared PVC, Deployment,
Service, Ingress, ConfigMap. Each is a `*.example.yaml`:

```sh
cp deploy/k8s/configmap.example.yaml deploy/k8s/configmap.yaml
# fill in real values, then:
kubectl apply -f deploy/k8s/configmap.yaml
```

The real (non-`.example`) copies are git-ignored, so cluster-specific values
never land in the repo. `configmap.example.yaml` already sets
`SLACK_LOG_PROFILE: "team"`.

## search.db is the data source

`indexer --profile team` reads `raw/slackdump.sqlite` once and fills the full
`search.db` schema — `messages` (FTS5) plus `message_raw`, `threads`,
`channels`, `users`. The server reads every page from those tables; there is
no `data/` jsonl directory and no `splitter` / `attach` step.

`search.db` is slack-log's own stable schema, decoupled from slackdump — see
[architecture.md](architecture.md#searchdb--slack-logs-own-stable-schema).

If you do want the jsonl layer too (for `grep` access on the server), set
`SLACK_LOG_EMIT_JSONL=1` — the refresh then also runs split + attach. Off by
default.

### First deploy / upgrade — rebuild search.db first

The team server reads the extended `search.db` schema (`channels` / `threads` /
`message_raw` / `users`). A `search.db` that is missing or was built by an
older version (FTS-only) makes content pages return 500 — `/healthz` still
passes, so the pod stays up, but pages fail until a refresh rebuilds it.

So on a first deploy or a version upgrade, **rebuild search.db before relying
on the service**: trigger `POST /sync` right after the rollout (or run
`indexer --profile team` against the volume first). The background scheduler
would also fix it within `SLACK_LOG_SYNC_INTERVAL`, but that can be an hour.

## Self-refresh

The server refreshes its own data — no external CronJob:

- a background scheduler runs the refresh every `SLACK_LOG_SYNC_INTERVAL`
  seconds (`0` disables it);
- `POST /sync` triggers an immediate refresh (bearer-token auth, the token is
  `SLACK_LOG_SYNC_TOKEN`); `GET /sync` reports status.

One in-process lock keeps the scheduler and the API from overlapping. The
refresh script is `scripts/refresh.sh`; it branches on `SLACK_LOG_PROFILE`.

The refresh is incremental: the first run does a full `slackdump archive`,
every run after does `slackdump resume`, which fetches only what changed
(with a one-week lookback for edits and late thread replies). The team
ETL then rebuilds search.db from the archive, and `attach` downloads only
files it doesn't already have.

## OIDC SSO

The archive holds real names and internal discussion, so a public deployment
must sit behind a login. Auth is **opt-in**: set all three of

```
OIDC_CLIENT_ID
OIDC_CLIENT_SECRET
OIDC_DISCOVERY_URL
```

and the server requires login (tested against authentik). Leave them unset and
it runs open, for local dev. `/healthz` is always public. `/sync` does its own
bearer-token check.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SLACK_LOG_PROFILE` | `personal` | set to `team` for this profile |
| `SLACK_LOG_ROOT` | `.` | base dir; `search.db` / `raw/` resolve under it |
| `SLACK_LOG_DB` | `<root>/search.db` | the database the server reads |
| `SLACK_LOG_INCLUDE` | all kinds | comma-separated subset of `channel,dm,mpim` |
| `SLACK_LOG_EMIT_JSONL` | off | also write the jsonl layer (escape hatch) |
| `SLACK_LOG_ATTACHMENTS` | on | download attachments during the refresh |
| `SLACK_LOG_ATTACHMENT_MAX_MB` | `10` | skip an attachment larger than this |
| `SLACK_LOG_SYNC_INTERVAL` | `0` | seconds between auto-syncs |
| `SLACK_LOG_SYNC_TOKEN` | — | bearer token for `POST /sync` |
| `SLACK_XOXC` / `SLACK_XOXD` | — | Slack credentials for the refresh |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_DISCOVERY_URL` | — | enable SSO |

`Config.from_env()` (`slack_log/config.py`) is the single place these are
parsed.

## Attachments

The team refresh downloads attachments by default. `attach` reads the file
list straight from search.db's `message_raw` table — no jsonl needed — and
stores files under `data/channels/<cid>/attachments/`, which the server
serves at `/channels/<cid>/attachments/<file>`.

Large files would balloon the volume, so `SLACK_LOG_ATTACHMENT_MAX_MB`
(default 10) caps per-file size: anything larger stays metadata-only and its
thread page links through to Slack instead. `SLACK_LOG_ATTACHMENTS=0` turns
downloading off entirely (every file becomes a Slack link).

The mime policy still applies on top of the cap: images / text / code / pdf
download; archives, video and audio are always metadata-only.

## CI/CD

Every push/PR runs the test matrix + lint; pushing a `vX.Y.Z` tag builds and
publishes the multi-arch image. See [CICD.md](CICD.md).

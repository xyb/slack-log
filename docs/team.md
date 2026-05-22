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

## Self-refresh

The server refreshes its own data — no external CronJob:

- a background scheduler runs the refresh every `SLACK_LOG_SYNC_INTERVAL`
  seconds (`0` disables it);
- `POST /sync` triggers an immediate refresh (bearer-token auth, the token is
  `SLACK_LOG_SYNC_TOKEN`); `GET /sync` reports status.

One in-process lock keeps the scheduler and the API from overlapping. The
refresh script is `scripts/refresh.sh`; it branches on `SLACK_LOG_PROFILE`.

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
| `SLACK_LOG_SYNC_INTERVAL` | `0` | seconds between auto-syncs |
| `SLACK_LOG_SYNC_TOKEN` | — | bearer token for `POST /sync` |
| `SLACK_XOXC` / `SLACK_XOXD` | — | Slack credentials for the refresh |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_DISCOVERY_URL` | — | enable SSO |

`Config.from_env()` (`slack_log/config.py`) is the single place these are
parsed.

## Attachments

The team profile does not download attachments by default (no jsonl to walk).
Thread pages link to attachments via their Slack permalink — visitors click
through to Slack. To serve attachments locally, run the refresh with
`SLACK_LOG_EMIT_JSONL=1` and point `SqliteStore` at the attachments root.

## CI/CD

Every push/PR runs the test matrix + lint; pushing a `vX.Y.Z` tag builds and
publishes the multi-arch image. See [CICD.md](CICD.md).

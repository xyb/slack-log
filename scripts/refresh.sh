#!/bin/sh
# Data-refresh pipeline — branches on SLACK_LOG_PROFILE.
#
#   team (the container default) — slackdump archive -> indexer ETL straight
#     into search.db. No jsonl, no attachment download; the server reads
#     search.db. SLACK_LOG_EMIT_JSONL=1 is an escape hatch that additionally
#     writes the jsonl layer (split + attach) — off by default.
#   personal — slackdump archive -> split -> attach -> index from the jsonl
#     layer; the server reads jsonl.
#
# Spawned by the server's in-process sync manager (slack_log/web/sync.py) — both
# the background scheduler and the POST /sync API run it. The sync manager
# guarantees only one refresh runs at a time, so this script needs no locking
# of its own. Writes the /data volume the same process serves from, so a
# successful run is immediately live.
#
# Credentials arrive as env vars from the K8s Secret:
#   SLACK_XOXC  — Slack browser token (xoxc-...)
#   SLACK_XOXD  — Slack cookie d value (xoxd-...)
# attach.py reads SLACK_XOXC / SLACK_XOXD directly; slackdump needs a
# "workspace" imported from those values (it won't read raw env vars).

set -eu

ROOT="${SLACK_LOG_ROOT:-/data}"
PROFILE="${SLACK_LOG_PROFILE:-personal}"
INCLUDE="${SLACK_LOG_INCLUDE:-channel}"
EMIT_JSONL="${SLACK_LOG_EMIT_JSONL:-}"
cd "$ROOT"
mkdir -p raw data
rm -rf html   # legacy static render output — server renders dynamically now

: "${SLACK_XOXC:?SLACK_XOXC required}"
: "${SLACK_XOXD:?SLACK_XOXD required}"

# slackdump 4.x authenticates via an imported workspace, not env vars. Write
# the credentials to a temp .env, import (no-encryption avoids machine-id
# coupling across pod restarts), delete the temp file, then archive.
echo "[refresh] $(date -u +%FT%TZ) profile=$PROFILE — slackdump workspace import..."
CREDS="$(mktemp)"
printf 'SLACK_TOKEN=%s\nSLACK_COOKIE=%s\n' "$SLACK_XOXC" "$SLACK_XOXD" > "$CREDS"
slackdump workspace import -no-encryption "$CREDS"
rm -f "$CREDS"

echo "[refresh] $(date -u +%FT%TZ) slackdump archive..."
# -files=false: slackdump archives messages only. Attachment downloads are
# handled by attach.py with its mime/size policy — letting slackdump pull
# every file would balloon the PVC (it tried 1148 dirs and filled 3Gi).
slackdump archive -no-encryption -files=false -o raw

# split + attach — writes the jsonl data layer. Best-effort attach: search and
# text browsing work without it, thread pages just show the image fallback.
split_and_attach() {
  echo "[refresh] split..."
  python3 -m slack_log.pipeline.split raw/slackdump.sqlite -o data
  echo "[refresh] attach (best-effort, 20min cap)..."
  timeout 1200 python3 -m slack_log.pipeline.attach data \
    || echo "[refresh] attach incomplete (timeout/error) — continuing"
}

if [ "$PROFILE" = "team" ]; then
  if [ -n "$EMIT_JSONL" ]; then
    echo "[refresh] SLACK_LOG_EMIT_JSONL set — also writing the jsonl layer"
    split_and_attach
  fi
  echo "[refresh] index (team ETL straight from slackdump.sqlite)..."
  python3 -m slack_log.pipeline.index --profile team --sqlite raw/slackdump.sqlite \
    --db search.db --include "$INCLUDE"
else
  split_and_attach
  echo "[refresh] index (personal, from the jsonl layer)..."
  python3 -m slack_log.pipeline.index --profile personal --data data \
    --db search.db --include "$INCLUDE"
fi

echo "[refresh] $(date -u +%FT%TZ) done."

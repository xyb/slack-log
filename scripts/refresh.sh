#!/bin/sh
# Data-refresh pipeline: slackdump archive -> split -> attach -> index.
#
# Spawned by the server's in-process sync manager (slack_log/sync.py) — both
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
INCLUDE="${SLACK_LOG_INCLUDE:-channel}"
cd "$ROOT"
mkdir -p raw data
rm -rf html   # legacy static render output — server renders dynamically now

: "${SLACK_XOXC:?SLACK_XOXC required}"
: "${SLACK_XOXD:?SLACK_XOXD required}"

# slackdump 4.x authenticates via an imported workspace, not env vars. Write
# the credentials to a temp .env, import (no-encryption avoids machine-id
# coupling across pod restarts), delete the temp file, then archive.
echo "[refresh] $(date -u +%FT%TZ) slackdump workspace import..."
CREDS="$(mktemp)"
printf 'SLACK_TOKEN=%s\nSLACK_COOKIE=%s\n' "$SLACK_XOXC" "$SLACK_XOXD" > "$CREDS"
slackdump workspace import -no-encryption "$CREDS"
rm -f "$CREDS"

echo "[refresh] $(date -u +%FT%TZ) slackdump archive..."
# -files=false: slackdump archives messages only. Attachment downloads are
# handled by attach.py with its mime/size policy — letting slackdump pull
# every file would balloon the PVC (it tried 1148 dirs and filled 3Gi).
slackdump archive -no-encryption -files=false -o raw

echo "[refresh] split..."
python3 -m slack_log.splitter raw/slackdump.sqlite -o data

echo "[refresh] attach (best-effort, 20min cap)..."
# Attachment download is non-blocking for the service: search + text browsing
# work without it, thread pages just show the image fallback. Cap the whole
# step so a slow/hung download can never stall the pipeline.
timeout 1200 python3 -m slack_log.attach data \
  || echo "[refresh] attach incomplete (timeout/error) — continuing"

# No render step: the server renders pages dynamically from the jsonl layer.
# (render.py is only for the static-flavor `make render-static` deploy.)

echo "[refresh] index..."
python3 -m slack_log.indexer --data data --db search.db --include "$INCLUDE"

echo "[refresh] $(date -u +%FT%TZ) done."

#!/bin/sh
# Data-refresh pipeline for the slack-log CronJob.
#
# Runs inside the container against the shared /data PVC. The web Deployment
# reads the same volume, so a successful run is immediately visible — no
# manual copy, no image rebuild.
#
# Credentials arrive as env vars from the K8s Secret:
#   SLACK_XOXC  — Slack browser token (xoxc-...)   → slackdump SLACK_TOKEN
#   SLACK_XOXD  — Slack cookie d value (xoxd-...)   → slackdump SLACK_COOKIE
# attach.py reads SLACK_XOXC / SLACK_XOXD directly.

set -eu

ROOT="${SLACK_LOG_ROOT:-/data}"
INCLUDE="${SLACK_LOG_INCLUDE:-channel}"
cd "$ROOT"
mkdir -p raw data html

: "${SLACK_XOXC:?SLACK_XOXC required}"
: "${SLACK_XOXD:?SLACK_XOXD required}"
export SLACK_TOKEN="$SLACK_XOXC"
export SLACK_COOKIE="$SLACK_XOXD"

echo "[refresh] $(date -u +%FT%TZ) slackdump archive..."
slackdump archive -o raw

echo "[refresh] split..."
python3 -m slack_log.splitter raw/slackdump.sqlite -o data

echo "[refresh] attach..."
python3 -m slack_log.attach data

echo "[refresh] render (flavor=server, include=$INCLUDE)..."
python3 -m slack_log.render --flavor server --include "$INCLUDE" --html html --data data

echo "[refresh] index..."
python3 -m slack_log.indexer --data data --db search.db --include "$INCLUDE"

echo "[refresh] $(date -u +%FT%TZ) done."

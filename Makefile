# slack-log build targets
#
# Layout:
#   raw/slackdump.sqlite  ← produced by slackdump archive (incremental resume)
#   data/                 ← splitter output + attach downloads (DO NOT wipe — attachments are slow to re-fetch)
#   html/                 ← render output (safe to wipe and regenerate)
#
# "Attachments are precious" principle: image downloads are slow, so HTML
# rebuilds must preserve data/. Only `make clean-all` removes data/.

PY ?= python3

.PHONY: help update fetch reconcile split attach render rebuild-html \
        render-channels render-dms render-mpims clean-html clean-all \
        index serve test

help:
	@echo "make update              full incremental: slackdump -> splitter -> attach -> render"
	@echo "make fetch               only run slackdump archive --resume (cheap, additive)"
	@echo "make reconcile           re-fetch last 90 days to pick up edits/deletes, then split + render"
	@echo "make split               SQLite -> jsonl + users.json + channels.json"
	@echo "make attach              walk jsonl files, download attachments by mime/size policy"
	@echo "make render              jsonl -> HTML (jinja2 + ref id + lightbox, all kinds)"
	@echo "make render-channels     only render channels (skip DMs and MPIMs)"
	@echo "make render-dms          only render DMs"
	@echo "make render-mpims        only render MPIMs"
	@echo "make rebuild-html        rebuild all HTML (preserves data/, fastest path)"
	@echo "make render-static       render the static (relative .html) flavor to html-static/"
	@echo "make index               build search.db (FTS5 full-text index over jsonl)"
	@echo "make index INCLUDE=channel   build a channels-only index (skips DM/MPIM)"
	@echo "make serve               run v0.7 web service (browse + search) on 127.0.0.1:8770"
	@echo "make serve-channels      same DB, runtime-filter to channels only"
	@echo "make test                run pytest"
	@echo "make clean-html          remove html/"
	@echo "make clean-all           ⚠ remove data/ + html/ (forces full re-split + re-download)"

update: fetch split attach render

fetch:
	cd raw && slackdump archive -o . --resume -files=false

# Weekly reconcile: pick up message edits and deletions. See README.
RECONCILE_DAYS ?= 90
reconcile:
	cd raw && slackdump archive -o . -files=false -member-only \
	    -time-from=$$($(PY) -c "from datetime import datetime, timedelta; print((datetime.now() - timedelta(days=$(RECONCILE_DAYS))).strftime('%Y-%m-%dT00:00:00'))") \
	    -chan-types=public_channel,private_channel,im,mpim
	$(PY) -m slack_log.splitter raw/slackdump.sqlite -o ./data
	$(PY) -m slack_log.attach ./data
	rm -rf html && $(PY) -m slack_log.render

split:
	$(PY) -m slack_log.splitter raw/slackdump.sqlite -o ./data

attach:
	$(PY) -m slack_log.attach ./data

render:
	rm -rf html
	$(PY) -m slack_log.render

render-channels: clean-html
	$(PY) -m slack_log.render --include=channel

# Static (no-server) flavor — relative .html links, drops in any web server.
render-static:
	rm -rf html-static
	$(PY) -m slack_log.render --flavor=static --html=./html-static --include=channel

render-dms: clean-html
	$(PY) -m slack_log.render --include=dm

render-mpims: clean-html
	$(PY) -m slack_log.render --include=mpim

# Most common: after template/CSS/render.py changes
rebuild-html: clean-html
	$(PY) -m slack_log.render

INCLUDE ?=
index:
	$(PY) -m slack_log.indexer --data ./data --db ./search.db $(if $(INCLUDE),--include $(INCLUDE))

PORT ?= 8770
HOST ?= 127.0.0.1
serve:
	$(PY) -m slack_log.server --db ./search.db --html ./html --data ./data --host $(HOST) --port $(PORT) $(if $(INCLUDE),--include $(INCLUDE))

# Channels-only flavor: same DB, server hides DM/MPIM at query time.
serve-channels:
	$(MAKE) serve INCLUDE=channel

test:
	$(PY) -m pytest

clean-html:
	rm -rf html

# ⚠ This wipes attachments — full re-download (~1300 images, ~15 min). Rarely used.
clean-all:
	rm -rf data html

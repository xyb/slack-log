# slack-log build targets — two product profiles.
#
#   personal — local use. The splitter writes the data/ jsonl layer and the
#              server reads jsonl. Run: make personal-build && make personal-serve
#   team     — server use. No jsonl: the indexer ETLs slackdump.sqlite straight
#              into search.db and the server reads search.db. Run:
#              make team-build && make team-serve
#
# data/ is precious — attachment downloads are slow, so only `make clean-all`
# removes it.

PY ?= python3
HOST ?= 127.0.0.1
PORT ?= 8770
INCLUDE ?=
MAX_MB ?= 10
_INCLUDE_ARG = $(if $(INCLUDE),--include $(INCLUDE),)

.PHONY: help \
        personal-build personal-serve render-static \
        team-build team-serve \
        fetch reconcile split attach index team-index team-attach \
        test clean-html clean-all

help:
	@echo "Personal profile — local, jsonl data layer:"
	@echo "  make personal-build      fetch -> split -> attach -> index"
	@echo "  make personal-serve      run the web service (reads data/ jsonl)"
	@echo "  make render-static       export static HTML to html-static/"
	@echo ""
	@echo "Team profile — server, search.db only (no jsonl):"
	@echo "  make team-build          fetch -> index (ETL) -> attach"
	@echo "  make team-serve          run the web service (reads search.db)"
	@echo "  (MAX_MB=N caps attachment size, default 10)"
	@echo ""
	@echo "Building blocks / misc:"
	@echo "  make fetch               slackdump archive --resume (cheap, additive)"
	@echo "  make reconcile           re-fetch last 90 days (pick up edits/deletes)"
	@echo "  make test                run pytest"
	@echo "  make clean-html          remove html/ + html-static/"
	@echo "  make clean-all           ⚠ also remove data/ + search.db"

# --- personal profile -----------------------------------------------------

personal-build: fetch split attach index

personal-serve:
	$(PY) -m slack_log.web.app --profile personal --db ./search.db --data ./data \
	    --host $(HOST) --port $(PORT) $(_INCLUDE_ARG)

render-static:
	rm -rf html-static
	$(PY) -m slack_log.web.static_export --flavor=static --html=./html-static --include=channel

# --- team profile ---------------------------------------------------------

team-build: fetch team-index team-attach

team-serve:
	$(PY) -m slack_log.web.app --profile team --db ./search.db \
	    --host $(HOST) --port $(PORT) $(_INCLUDE_ARG)

# --- building blocks ------------------------------------------------------

# First run does a full archive; every run after resumes — slackdump fetches
# only the delta since last time (1-week lookback for edits / late replies).
fetch:
	mkdir -p raw
	@if [ -f raw/slackdump.sqlite ]; then \
	    echo "→ slackdump resume (incremental)"; \
	    cd raw && slackdump resume -files=false . ; \
	else \
	    echo "→ slackdump archive (first run, full)"; \
	    cd raw && slackdump archive -o . -files=false ; \
	fi

# Weekly reconcile: pick up message edits and deletions. See docs/.
RECONCILE_DAYS ?= 90
reconcile:
	mkdir -p raw
	cd raw && slackdump archive -o . -files=false -member-only \
	    -time-from=$$($(PY) -c "from datetime import datetime, timedelta; print((datetime.now() - timedelta(days=$(RECONCILE_DAYS))).strftime('%Y-%m-%dT00:00:00'))") \
	    -chan-types=public_channel,private_channel,im,mpim

split:
	$(PY) -m slack_log.pipeline.split raw/slackdump.sqlite -o ./data

# personal: download attachments by walking the jsonl layer (MAX_MB size cap)
attach:
	$(PY) -m slack_log.pipeline.attach ./data --max-mb $(MAX_MB)

# personal: index from the jsonl layer
index:
	$(PY) -m slack_log.pipeline.index --profile personal --data ./data --db ./search.db $(_INCLUDE_ARG)

# team: ETL straight from slackdump.sqlite — no jsonl in between
team-index:
	$(PY) -m slack_log.pipeline.index --profile team --sqlite raw/slackdump.sqlite --db ./search.db $(_INCLUDE_ARG)

# team: download attachments by walking search.db's message_raw (no jsonl)
team-attach:
	$(PY) -m slack_log.pipeline.attach ./data --sqlite ./search.db --max-mb $(MAX_MB)

test:
	$(PY) -m pytest

clean-html:
	rm -rf html html-static

# ⚠ This wipes attachments — full re-download (~1300 images, ~15 min). Rarely used.
clean-all:
	rm -rf data html html-static search.db

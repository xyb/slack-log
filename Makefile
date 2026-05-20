# slack-log build targets
#
# Layout:
#   raw/slackdump.sqlite  ← produced by slackdump archive (incremental resume)
#   data/                 ← splitter output + attach downloads (DO NOT wipe — attachments are slow to re-fetch)
#   html/                 ← render output (safe to wipe and regenerate)
#
# "Attachments are precious" principle: image downloads are slow, so HTML
# rebuilds must preserve data/. Only `make clean-all` removes data/.

.PHONY: help update fetch split attach render rebuild-html render-channels render-dms render-mpims clean-html clean-all

help:
	@echo "make update              full incremental: slackdump -> splitter -> attach -> render"
	@echo "make fetch               only run slackdump archive --resume"
	@echo "make split               SQLite -> jsonl + users.json + channels.json"
	@echo "make attach              walk jsonl files, download attachments by mime/size policy"
	@echo "make render              jsonl -> HTML (jinja2 + ref id + lightbox, all kinds)"
	@echo "make render-channels     only render channels (skip DMs and MPIMs)"
	@echo "make render-dms          only render DMs"
	@echo "make render-mpims        only render MPIMs"
	@echo "make rebuild-html        rebuild all HTML (preserves data/, fastest path)"
	@echo "make clean-html          remove html/"
	@echo "make clean-all           ⚠ remove data/ + html/ (forces full re-split + re-download)"

update: fetch split attach render

fetch:
	cd raw && slackdump archive -o . --resume -files=false

split:
	python3 splitter.py raw/slackdump.sqlite -o ./data

attach:
	python3 attach.py ./data

render:
	rm -rf html
	python3 render.py

render-channels: clean-html
	python3 render.py --include=channel

render-dms: clean-html
	python3 render.py --include=dm

render-mpims: clean-html
	python3 render.py --include=mpim

# Most common: after template/CSS/render.py changes
rebuild-html: clean-html
	python3 render.py

clean-html:
	rm -rf html

# ⚠ This wipes attachments — full re-download (~1300 images, ~15 min). Rarely used.
clean-all:
	rm -rf data html

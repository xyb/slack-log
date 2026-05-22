"""Pipeline — the data-processing stages that turn a Slack archive into
search.db (and, for the personal profile, the data/ jsonl layer).

  split   slackdump.sqlite → per-thread jsonl (personal profile)
  attach  download attachments by mime/size policy
  index   build search.db — the FTS5 index plus the team ETL

The static-HTML exporter lives in web/static_export.py — it renders the same
Jinja templates the server does, so it belongs with the serving layer.
"""

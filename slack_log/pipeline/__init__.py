"""Pipeline — the data-processing stages that turn a Slack archive into
search.db (and, for the personal profile, the data/ jsonl layer).

  split   slackdump.sqlite → per-thread jsonl (personal profile)
  attach  download attachments by mime/size policy
  index   build search.db — the FTS5 index plus the team ETL
  render  shared render helpers + the static-HTML exporter
"""

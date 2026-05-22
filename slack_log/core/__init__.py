"""Shared core layer — used by every product profile (personal / team).

  slackdump_db — read slackdump's archive SQLite (dedup, users, channels, bots)
  text         — Slack text processing (mention/link/mrkdwn/emoji/CJK)

These have no dependency on a particular storage backend or profile, so both
the pipeline modules and the store implementations build on them.
"""

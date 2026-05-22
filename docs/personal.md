# Personal profile

A local archive of your own Slack. The point is the **`data/` JSONL layer** —
one file per thread, full Slack fields preserved — that you can `grep`, pipe
through `jq`, or feed to an AI. A local web server and a static-HTML export are
both available on top of it.

## Install

```sh
brew install slackdump      # the collection layer
pip install -e .

# Give slackdump your Slack credentials (xoxc + xoxd from browser cookies).
# Easy way: https://github.com/rusq/slackdump/wiki/EZ-Login-3000
slackdump workspace new
```

## Build

```sh
make personal-build
```

That runs four steps — each is also a target of its own:

| Step | Command | Output |
|------|---------|--------|
| fetch  | `make fetch`   | `raw/slackdump.sqlite` — slackdump archives the workspace |
| split  | `make split`   | `data/channels/<cid>/threads/<thread_ts>.jsonl` + `index.jsonl` + `users.json` + `channels.json` |
| attach | `make attach`  | `data/channels/<cid>/attachments/` — downloads by mime/size policy |
| index  | `make index`   | `search.db` — the FTS5 full-text index |

`make fetch` is incremental (slackdump `--resume`) — cheap to run often.

## The data layer

```
data/
  channels/<cid>/
    threads/<thread_ts>.jsonl   one thread, one complete message per line
    index.jsonl                 per-thread metadata, one line per thread
    attachments/                downloaded files + <id>.meta.json
  users.json                    {uid: profile}
  channels.json                 {cid: meta}
```

A thread jsonl line is a raw Slack message — `blocks`, `reactions`, `files`,
`edited`, `attachments` all preserved. Files are named by `thread_ts`, Slack's
stable unique id, so a path never changes.

```sh
# every message that mentions a deploy
grep -rl deploy data/channels/*/threads/

# who posted in a thread
jq -r .user data/channels/C0XXXX/threads/1779079280.797169.jsonl

# feed a whole thread to an AI
cat data/channels/C0XXXX/threads/1779079280.797169.jsonl
```

## Browse it

```sh
make personal-serve      # → http://127.0.0.1:8770
```

The server renders every page dynamically from the jsonl layer — channel
lists, threads, per-user timelines, attachments — with FTS5 search. No login.

Or export plain static HTML, for `file://` or any static host:

```sh
make render-static       # → html-static/
```

The static export carries `.html` suffixes and relative links; the server
flavor uses clean suffixless URLs. Static export omits search and per-user
pages (those need the backend).

## Keeping it fresh

`make fetch` (or `make personal-build`) pulls new messages. Slack does not
push edits and deletes over the archive API, so once a week:

```sh
make reconcile           # re-fetch the last 90 days
make personal-build      # then rebuild
```

The splitter dedups by `MAX(LOAD_DTTM)`, so the latest version of every
message wins.

## Credentials for attach

`slackdump` handles its own auth. `attach.py` additionally needs your Slack
browser token and cookie to download private file attachments. Resolution
order:

1. `SLACK_XOXC` + `SLACK_XOXD` environment variables
2. `./.env` in the current directory
3. `~/.config/slack-log/.env` (XDG-respecting)

```sh
mkdir -p ~/.config/slack-log
cat > ~/.config/slack-log/.env <<EOF
SLACK_XOXC=xoxc-your-token
SLACK_XOXD=xoxd-your-cookie
EOF
chmod 600 ~/.config/slack-log/.env
```

Standard `.env` format, parsed by
[python-dotenv](https://github.com/theskumar/python-dotenv).

## Notes

- `data/` is precious — attachment downloads are slow. Only `make clean-all`
  removes it; `make clean-html` leaves it alone.
- A big zip or video gets a `.meta.json` only (with the original Slack URL) —
  not the file itself. `make attach MAX_MB=N` sets the size cap (default 10);
  anything larger stays metadata-only.

# Claude Code Explorer

A tiny local viewer for your `~/.claude/` directory — Claude Code's config, sessions,
skills, agents, and token usage. Runs entirely on your machine.

## Run it

From this folder, start the server:

```bash
python3 server.py          # start (Ctrl-C to stop in the foreground)
python3 server.py stop     # stop a running instance
python3 server.py restart  # stop, then start a fresh instance
```

## Requirements

- macOS with **Python 3** (`python3`).

## Notes

- Read-only. The server only ever reads files, and it's confined to `~/.claude`
  (path-traversal attempts are rejected) and bound to `127.0.0.1` (localhost only).
- It always browses the current user's own `~/.claude`, so each person sees their own data.
- Port is `8777`. If it's already in use, launching again just opens the existing instance.

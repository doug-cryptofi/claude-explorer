# Claude Explorer

A tiny local viewer for your `~/.claude/` directory. Runs entirely on your machine.

## Run it

From this folder, start the server:

```bash
python3 server.py
```

## Requirements

- macOS with **Python 3** (`python3`). Most machines with developer tools already
  have it. If not, install via
  [python.org](https://www.python.org/downloads/) or `xcode-select --install`.
- No third-party packages — it uses only the Python standard library.

## Notes

- Read-only. The server only ever reads files, and it's confined to `~/.claude`
  (path-traversal attempts are rejected) and bound to `127.0.0.1` (localhost only).
- It always browses the current user's own `~/.claude`, so each person sees their own data.
- Port is `8777`. If it's already in use, launching again just opens the existing instance.

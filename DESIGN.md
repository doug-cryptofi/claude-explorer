# Design: durable usage archive + git cross-reference

Status: proposed (not yet built). Two related features that make the Usage
dashboard resilient to transcript pruning and correlate usage with git activity.

## Portability / sharing constraints

This app is distributed for other people to run on their own machines. Every
part of this design must therefore be **per-user and zero-config**:

- No hardcoded paths, home directories, emails, repo names, or org names.
  Resolve `~/.claude` via the existing `ROOT` (`os.path.expanduser`).
- All persisted state lives under the user's own `~/.claude`, so each user gets
  an independent archive with no shared/global assumptions.
- Git identity is auto-detected per repo (see Part B) — never configured.
- Nothing requires network access or `gh` auth to function; `gh` is an optional
  enhancement that degrades gracefully when absent or unauthenticated.
- Stdlib only (`sqlite3`), consistent with the app's dependency-free stance.

---

## The problem

`compute_usage()` (server.py) walks `~/.claude/projects/**/*.jsonl` and returns
`by_day`, `by_model`, `by_project` rows plus `totals`/`health`. Claude Code
deletes transcripts older than `cleanupPeriodDays` (default 30) on startup,
stamping `~/.claude/.last-cleanup`. Once a transcript is pruned, that history is
gone from the dashboard permanently. We should not try to race the pruner — we
should persist our own rollups so pruning becomes a non-event.

---

## Part A — SQLite persistence (survive pruning)

### Storage

- Stdlib `sqlite3`. DB at `~/.claude/.explorer-usage.db`.
- Dot-prefixed so the file browser can hide it; lives outside `projects/` so
  Claude Code never prunes it.
- Created on first run; each user has their own.

### Schema

One fact table at day × model × project grain (project is the dimension both
features share):

```sql
CREATE TABLE IF NOT EXISTS usage_daily (
  date         TEXT NOT NULL,   -- 'YYYY-MM-DD'
  model        TEXT NOT NULL,
  project      TEXT NOT NULL,
  input        INTEGER NOT NULL DEFAULT 0,
  output       INTEGER NOT NULL DEFAULT 0,
  cache_read   INTEGER NOT NULL DEFAULT 0,
  cache_create INTEGER NOT NULL DEFAULT 0,
  cost         REAL    NOT NULL DEFAULT 0,
  messages     INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT    NOT NULL,
  PRIMARY KEY (date, model, project)
);
```

`by_day`, `by_model`, `by_project`, and `totals` are all `GROUP BY` rollups of
this single table — nothing is stored redundantly.

**What cannot be archived:** session-level and percentile health stats
(`ctx_p95`, `retry_rate`, top-session share, `sessions`, etc.) depend on
per-message / per-session data that can't be reconstructed from a daily rollup.
Those remain **live-only** and reflect the current transcript window. The UI
labels the health cards as "current window" so it's clear they don't extend to
archived history.

### Ingestion (upsert-on-compute)

At the end of `compute_usage()`, after building the rows, upsert each
day×model×project cell:

```sql
INSERT INTO usage_daily (date, model, project, input, output, cache_read,
                         cache_create, cost, messages, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date, model, project) DO UPDATE SET
  input=excluded.input, output=excluded.output,
  cache_read=excluded.cache_read, cache_create=excluded.cache_create,
  cost=excluded.cost, messages=excluded.messages,
  updated_at=excluded.updated_at;
```

- Idempotent: today's partial day is overwritten with fresher numbers on each
  run; past days settle once their transcripts stop changing.
- Cheap: the existing signature cache means this only fires when a transcript
  actually changes.
- To upsert, `compute_usage()` must retain day×model×project granularity
  internally (currently it aggregates `by_day` and `by_model` separately). Add a
  combined accumulator keyed `(day, model, project)` and derive the existing
  rollups from it, so the live response and the archive come from one source.

### Serving

`/api/usage` returns the **union**:

- `by_day` / `by_model` / `by_project` ← `GROUP BY` over `usage_daily` (archive
  plus the just-upserted recent rows = full history).
- `totals` ← summed from the same query so it always matches the chart.
- `health` + `sessions` ← from the live transcript pass (window-only), labeled.

Historical `cost` is frozen at whatever pricing was in effect when the row was
written — correct for "what did I actually spend then," even if pricing changes.

### UI

- The per-day chart already scrolls horizontally, so longer history just
  extends it. Add a range control (30d / 90d / All).
- Note in the UI that data before first run isn't backfillable — we only capture
  from the day the DB starts accumulating.

### Prune-awareness banner (cheap add-on)

Read `~/.claude/.last-cleanup` and `cleanupPeriodDays` (default 30). Show
"N transcripts leave the live window in ~X days — archived copies are safe."
Uses only files already on disk; reframes pruning as a non-event.

---

## Part B — git cross-reference

Built on the same `project` dimension as Part A.

### Project → repo mapping

Usage `project` labels come from encoded cwd paths (e.g.
`-Users-<name>-Documents-repos-<repo>`). Decode `-` → `/` to recover the repo
path, confirm it is a git repo (`.git` present), and cache the map. Skip any
project that doesn't resolve to a git repo.

### Git identity (resolved)

**Use each repo's local `git config user.email` to identify the author.**

- It is the identity that actually authored the local commits.
- No network, no `gh` auth, works offline.
- Inherently per-user and portable — every user who runs the app gets their own
  correct identity with zero configuration, which is essential for a shared app.
- Fallback: if a repo has no `user.email` set, try `gh api user` (email/login);
  if that is also unavailable, count all commits and label the series
  "all authors" for that repo rather than failing.

### Data collection (per repo, per day)

- **Local git (primary):**
  `git log --author=<email> --since=<start> --pretty=%cI` → commits/day.
  Covers unpushed work; real commit timestamps.
- **`gh` (optional):**
  `gh pr list --author @me --state merged --json mergedAt,repository`
  → PRs-merged markers. Better "shipped" signal than raw commits. Skipped
  silently if `gh` is missing or unauthenticated.

Persist into a parallel table so the correlation history is durable and we don't
shell out on every page load:

```sql
CREATE TABLE IF NOT EXISTS git_daily (
  date       TEXT NOT NULL,
  project    TEXT NOT NULL,
  commits    INTEGER NOT NULL DEFAULT 0,
  prs_merged INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (date, project)
);
```

### UI

- Overlay a **commits/day line** on the existing per-day bar chart (second Y
  axis), with optional PR-merged dots.
- A **"cost per commit"** stat tile.
- Per-project rows showing spend alongside commit count.

### Caveats (surfaced in-UI, not hidden)

- Path → repo mapping breaks on renamed/moved dirs and monorepos.
- Squash-merges collapse commit counts.
- Plenty of Claude work never lands as a commit.

So it is presented as **correlation, not attribution**.

---

## Suggested build order

1. **Part A** — schema + upsert + union read + range control. Foundational;
   everything durable flows from the one table.
2. **Prune-awareness banner** — tiny, high value, uses files already on disk.
3. **Part B** — git overlay. Most meaningful once there is >30 days of archived
   history to correlate against.

## Notes for a shared release

- Ship the DB as create-on-first-run; never commit a DB to the repo.
- Add `.explorer-usage.db` to `.gitignore` if any dev points `ROOT` at a repo.
- Document in the README that history begins accumulating from first launch.

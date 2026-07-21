#!/usr/bin/env python3
"""Claude Code Explorer — a local file + usage viewer for ~/.claude/

Serves a browser UI at http://localhost:8777 that lets you navigate the
directory tree and view file contents. Read-only. Confined to ROOT.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.expanduser("~/.claude")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8777
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB cap for viewing


def safe_path(rel):
    """Resolve rel against ROOT, refusing anything that escapes ROOT."""
    rel = unquote(rel or "").lstrip("/")
    target = os.path.realpath(os.path.join(ROOT, rel))
    root_real = os.path.realpath(ROOT)
    if target != root_real and not target.startswith(root_real + os.sep):
        return None
    return target


MEMORY_TYPES = {"user", "feedback", "project", "reference"}


def is_memory_file(path):
    """A memory file is a .md with YAML frontmatter carrying a known
    memory `type:` or an `originSessionId:`."""
    if not path.endswith(".md"):
        return False
    if os.path.basename(path) == "MEMORY.md":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    text = head.decode("utf-8", "replace")
    if not text.lstrip().startswith("---"):
        return False
    # inspect the frontmatter block (first chunk is enough)
    for line in text.splitlines()[1:]:
        s = line.strip()
        if s == "---":
            break
        if s.startswith("type:") and s[5:].strip() in MEMORY_TYPES:
            return True
        if s.startswith("originSessionId:"):
            return True
    return False


def path_categories(rel):
    """Category tags derivable purely from a path relative to ROOT."""
    parts = [p for p in rel.split("/") if p] if rel else []
    segs = set(parts)
    base = parts[-1] if parts else ""
    cats = set()
    if "skills" in segs or base == "SKILL.md":
        cats.add("skills")
    if "agents" in segs or "agent-memory" in segs:
        cats.add("agents")
    if "commands" in segs:
        cats.add("commands")
    if "plugins" in segs:
        cats.add("plugins")
    if "projects" in segs or "sessions" in segs:
        cats.add("sessions")
    # top-level *.json config files (settings.json, *.json at the root)
    if len(parts) == 1 and base.endswith(".json"):
        cats.add("config")
    return cats


def file_categories(abspath, rel):
    cats = path_categories(rel)
    if is_memory_file(abspath):
        cats.add("memory")
    return cats


def dir_subtree_categories(dirpath, rel, budget=4000):
    """Categories present in a directory's subtree (bounded walk),
    unioned with the directory's own path-based categories."""
    cats = set(path_categories(rel))
    stack = [(dirpath, rel)]
    scanned = 0
    while stack:
        d, drel = stack.pop()
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            full = os.path.join(d, n)
            crel = drel + "/" + n if drel else n
            if os.path.isdir(full):
                cats |= path_categories(crel)
                stack.append((full, crel))
            else:
                scanned += 1
                if scanned > budget:
                    return cats
                cats |= file_categories(full, crel)
    return cats


def list_dir(abspath, rel):
    entries = []
    try:
        names = os.listdir(abspath)
    except OSError as e:
        return {"error": str(e)}
    for name in names:
        full = os.path.join(abspath, name)
        crel = rel + "/" + name if rel else name
        try:
            st = os.stat(full)
            is_dir = os.path.isdir(full)
            entry = {
                "name": name,
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else st.st_size,
                "mtime": int(st.st_mtime),
            }
            cats = dir_subtree_categories(full, crel) if is_dir else file_categories(full, crel)
            entry["cats"] = sorted(cats)
            entries.append(entry)
        except OSError:
            entries.append({"name": name, "type": "unknown", "size": 0, "mtime": 0, "cats": []})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"entries": entries}


# --- token-usage aggregation over projects/**/*.jsonl transcripts ---

# API list prices, USD per 1M tokens: (input, output, cache_read, write_5m, write_1h).
# Cache rates are the published multiples of input: read 0.1x, 5m-write 1.25x, 1h-write 2x.
# 1M-context usage carries NO long-context premium on Opus 4.7/4.8, so the [1m] model
# suffix does not change pricing — it is treated as the same tier.
_PRICES = {
    "opus":        (5.0, 25.0, 0.5, 6.25, 10.0),    # Opus 4.5 / 4.6 / 4.7 / 4.8
    "opus_legacy": (15.0, 75.0, 1.5, 18.75, 30.0),  # Opus 3 / 4.0 / 4.1
    "fable":       (10.0, 50.0, 1.0, 12.5, 20.0),   # Fable 5 / Mythos 5
    "sonnet":      (3.0, 15.0, 0.3, 3.75, 6.0),
    "haiku":       (1.0, 5.0, 0.10, 1.25, 2.0),
}

_OPUS_VER = re.compile(r"opus-(\d+)-(\d+)")


def _is_opus(model):
    return "opus" in (model or "").lower()


def _model_prices(model):
    """Per-model list prices, keyed on family and (for Opus) version — the
    $5/$25 tier began at Opus 4.5; older Opus was $15/$75."""
    m = (model or "").lower()
    if "haiku" in m:
        return _PRICES["haiku"]
    if "sonnet" in m:
        return _PRICES["sonnet"]
    if "fable" in m or "mythos" in m:
        return _PRICES["fable"]
    ver = _OPUS_VER.search(m)
    if ver and (int(ver.group(1)), int(ver.group(2))) < (4, 5):
        return _PRICES["opus_legacy"]
    if "-3-opus" in m or "opus-3" in m:
        return _PRICES["opus_legacy"]
    return _PRICES["opus"]  # current Opus / unknown


def _usage_signature():
    """Cheap fingerprint of the transcript set (path+mtime+size) so we can
    cache the aggregation and only recompute when a transcript changes."""
    proj = os.path.join(ROOT, "projects")
    sig = []
    for dirpath, _dirs, files in os.walk(proj):
        for n in files:
            if n.endswith(".jsonl"):
                full = os.path.join(dirpath, n)
                try:
                    st = os.stat(full)
                    sig.append((full, int(st.st_mtime), st.st_size))
                except OSError:
                    pass
    sig.sort()
    return tuple(sig)


_usage_cache = {"sig": None, "data": None}


def _blank():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "messages": 0}


def _add(acc, it, ot, cr, cc, cost):
    acc["input"] += it
    acc["output"] += ot
    acc["cache_read"] += cr
    acc["cache_create"] += cc
    acc["cost"] += cost
    acc["messages"] += 1


def compute_usage():
    sig = _usage_signature()
    if _usage_cache["sig"] == sig and _usage_cache["data"] is not None:
        return _usage_cache["data"]

    totals = _blank()
    by_model, by_project, by_day = {}, {}, {}
    session_cost = {}      # sessionId -> total cost
    opus_cost = 0.0        # cost attributable to Opus models
    retries = 0            # messages that took >1 inference iteration
    synthetic = 0          # <synthetic> messages (cancels/errors)
    ctx_samples = []       # per-message input-side token totals (context weight)
    first = last = None

    proj_root = os.path.join(ROOT, "projects")
    for dirpath, _dirs, files in os.walk(proj_root):
        for n in files:
            if not n.endswith(".jsonl"):
                continue
            proj_label = None
            with open(os.path.join(dirpath, n), "r", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if proj_label is None and d.get("cwd"):
                        proj_label = os.path.basename(d["cwd"])
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not u:
                        continue
                    it = u.get("input_tokens", 0) or 0
                    ot = u.get("output_tokens", 0) or 0
                    cr = u.get("cache_read_input_tokens", 0) or 0
                    cc = u.get("cache_creation_input_tokens", 0) or 0
                    split = u.get("cache_creation") or {}
                    w5 = split.get("ephemeral_5m_input_tokens", cc) or 0
                    w1 = split.get("ephemeral_1h_input_tokens", 0) or 0
                    if not split:
                        w5, w1 = cc, 0
                    model = msg.get("model") or "unknown"
                    p = _model_prices(model)
                    cost = (it * p[0] + ot * p[1] + cr * p[2]
                            + w5 * p[3] + w1 * p[4]) / 1_000_000
                    if _is_opus(model):
                        opus_cost += cost
                    if model == "<synthetic>":
                        synthetic += 1
                    if len(u.get("iterations") or []) > 1:
                        retries += 1
                    ctx_samples.append(it + cr + cc)
                    sid = d.get("sessionId")
                    if sid:
                        session_cost[sid] = session_cost.get(sid, 0.0) + cost

                    ts = d.get("timestamp") or ""
                    day = ts[:10]
                    if ts:
                        first = ts if first is None or ts < first else first
                        last = ts if last is None or ts > last else last

                    label = proj_label or os.path.basename(dirpath)
                    _add(totals, it, ot, cr, cc, cost)
                    _add(by_model.setdefault(model, _blank()), it, ot, cr, cc, cost)
                    _add(by_project.setdefault(label, _blank()), it, ot, cr, cc, cost)
                    if day:
                        _add(by_day.setdefault(day, _blank()), it, ot, cr, cc, cost)

    def rows(mapping, key_name):
        out = []
        for k, v in mapping.items():
            row = {key_name: k}
            row.update(v)
            out.append(row)
        return out

    model_rows = sorted(rows(by_model, "model"), key=lambda r: -r["cost"])
    project_rows = sorted(rows(by_project, "project"), key=lambda r: -r["cost"])
    day_rows = sorted(rows(by_day, "date"), key=lambda r: r["date"])

    def pct(sorted_vals, q):
        if not sorted_vals:
            return 0
        return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]

    n_msgs = totals["messages"] or 1
    n_sess = len(session_cost) or 1
    total_cost = totals["cost"] or 1e-9
    input_side = totals["input"] + totals["cache_read"] + totals["cache_create"]
    sess_costs = sorted(session_cost.values(), reverse=True)
    day_costs = sorted((r["cost"] for r in day_rows), reverse=True)
    ctx_samples.sort()

    health = {
        "cache_hit_rate": totals["cache_read"] / (input_side or 1),
        "cache_reuse": totals["cache_read"] / (totals["cache_create"] or 1),
        "opus_cost_share": opus_cost / total_cost,
        "cost_per_session": totals["cost"] / n_sess,
        "cost_per_msg": totals["cost"] / n_msgs,
        "top_session_cost": sess_costs[0] if sess_costs else 0.0,
        "top_session_share": (sess_costs[0] / total_cost) if sess_costs else 0.0,
        "top5_session_share": sum(sess_costs[:5]) / total_cost,
        "top_day_share": (day_costs[0] / total_cost) if day_costs else 0.0,
        "retry_rate": retries / n_msgs,
        "synthetic": synthetic,
        "ctx_p50": pct(ctx_samples, 0.50),
        "ctx_p95": pct(ctx_samples, 0.95),
        "ctx_max": ctx_samples[-1] if ctx_samples else 0,
        "high_ctx_share": sum(1 for c in ctx_samples if c > 150000) / n_msgs,
    }

    data = {
        "range": {"start": (first or "")[:10], "end": (last or "")[:10]},
        "sessions": len(session_cost),
        "totals": totals,
        "health": health,
        "by_model": model_rows,
        "by_project": project_rows,
        "by_day": day_rows,
    }
    _usage_cache["sig"] = sig
    _usage_cache["data"] = data
    return data


# --- plan / billing descriptor (so the cost figure can be labelled honestly) ---

_RATE_TIER_LABELS = {
    "default_claude_max_20x": "Claude Max 20×",
    "default_claude_max_5x": "Claude Max 5×",
    "default_claude_pro": "Claude Pro",
    "default_claude_free": "Claude Free",
}
_SEAT_LABELS = {"team_tier_1": "Team", "enterprise": "Enterprise"}


def _load_config():
    """Load the user's Claude config (~/.claude.json, else newest backup).
    Returns the parsed dict, or {} if none is readable."""
    candidates = [os.path.expanduser("~/.claude.json")]
    candidates += sorted(
        glob.glob(os.path.join(ROOT, "backups", ".claude.json.backup.*")),
        reverse=True,
    )
    for path in candidates:
        try:
            with open(path, "r", errors="replace") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and cfg.get("oauthAccount"):
            return cfg
    return {}


def read_plan():
    """Extract only the plan-descriptive fields from the user's Claude config.
    Never touches tokens, email, or account identifiers."""
    cfg = _load_config()
    acct = cfg.get("oauthAccount") or {}
    if not acct:
        return None
    billing = acct.get("billingType") or ""
    rate = acct.get("userRateLimitTier") or ""
    seat = acct.get("seatTier") or ""
    org = acct.get("organizationRateLimitTier") or ""
    metered = billing not in ("stripe_subscription",)
    tier_label = _RATE_TIER_LABELS.get(rate) or (rate.replace("default_", "").replace("_", " ") if rate else "")
    parts = [p for p in (tier_label, _SEAT_LABELS.get(seat, "")) if p]
    # rate-limit promo notices (text only — no account data)
    promos = []
    feats = cfg.get("cachedGrowthBookFeatures") or {}
    for note in (feats.get("tengu_rate_limit_promo_notices") or []):
        if isinstance(note, dict) and note.get("text"):
            promos.append(note["text"])
    return {
        "label": " · ".join(parts) if parts else "Subscription",
        "billing": billing,
        "seat_tier": _SEAT_LABELS.get(seat, seat),
        "rate_tier": tier_label or rate,
        "org_tier": org.replace("default_", "").replace("_", " ") if org else "",
        "metered": metered,
        "extra_usage": bool(acct.get("hasExtraUsageEnabled")),
        "since": (acct.get("subscriptionCreatedAt") or "")[:10],
        "promos": promos,
    }


def read_activity():
    """Skill / tool / plugin usage counts from the Claude config. Counts and
    last-used timestamps only — no prompts, no message content."""
    cfg = _load_config()
    if not cfg:
        return None

    def top(mapping, n=12):
        rows = []
        for name, v in (mapping or {}).items():
            if isinstance(v, dict) and v.get("usageCount"):
                rows.append({
                    "name": name,
                    "count": v.get("usageCount", 0),
                    "last": v.get("lastUsedAt"),
                })
        rows.sort(key=lambda r: -r["count"])
        return rows[:n]

    return {
        "skills": top(cfg.get("skillUsage")),
        "tools": top(cfg.get("toolUsage")),
        "plugins": top(cfg.get("pluginUsage")),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Restrictive CSP: no external script/resource loads, and same-origin-only
    # network access so a rendering bug can't exfiltrate data off-box.
    CSP = ("default-src 'none'; img-src 'self' data: http: https:; "
           "style-src 'self' 'unsafe-inline'; script-src 'self'; "
           "connect-src 'self'; base-uri 'none'; form-action 'none'")

    def _send_file(self, path, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        rel = qs.get("path", [""])[0]

        if route in ("/", "/index.html"):
            return self._send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")

        if route == "/app.js":
            return self._send_file(os.path.join(HERE, "app.js"), "application/javascript; charset=utf-8")

        if route == "/styles.css":
            return self._send_file(os.path.join(HERE, "styles.css"), "text/css; charset=utf-8")

        if route == "/api/root":
            return self._send_json({"root": ROOT})

        if route == "/api/open":
            target = safe_path(rel)
            if target is None or not os.path.exists(target):
                return self._send_json({"error": "not found or forbidden"}, 400)
            code_bin = shutil.which("code")
            try:
                if code_bin:
                    subprocess.Popen([code_bin, target])
                else:
                    subprocess.Popen(["open", "-a", "Visual Studio Code", target])
                return self._send_json({"ok": True})
            except OSError as e:
                return self._send_json({"error": str(e)}, 500)

        if route == "/api/usage":
            try:
                # copy so the plan/activity (read fresh) don't get baked into the cache
                out = dict(compute_usage())
                out["plan"] = read_plan()
                out["activity"] = read_activity()
                return self._send_json(out)
            except OSError as e:
                return self._send_json({"error": str(e)}, 500)

        if route == "/api/list":
            target = safe_path(rel)
            if target is None or not os.path.isdir(target):
                return self._send_json({"error": "not a directory or forbidden"}, 400)
            return self._send_json(list_dir(target, rel.strip("/")))

        if route == "/api/file":
            target = safe_path(rel)
            if target is None or not os.path.isfile(target):
                return self._send_json({"error": "not a file or forbidden"}, 400)
            size = os.path.getsize(target)
            if size > MAX_FILE_BYTES:
                return self._send_json({
                    "truncated": True, "size": size,
                    "content": "[file too large to display: %d bytes]" % size,
                })
            with open(target, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode("utf-8")
                return self._send_json({"content": text, "size": size, "binary": False})
            except UnicodeDecodeError:
                return self._send_json({
                    "binary": True, "size": size,
                    "content": "[binary file: %d bytes]" % size,
                })

        return self._send_json({"error": "not found"}, 404)


def main():
    os.chdir(HERE)
    url = "http://localhost:%d" % PORT
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Port already in use — an instance is likely running. Just open it.
        print("Port %d is busy; opening the existing instance at %s" % (PORT, url))
        if os.environ.get("CX_NO_OPEN") != "1":
            webbrowser.open(url)
        return
    print("Claude Code Explorer serving %s" % ROOT)
    print("Open %s   (press Ctrl-C to stop)" % url)
    if os.environ.get("CX_NO_OPEN") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

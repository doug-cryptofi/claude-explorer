#!/usr/bin/env python3
"""Simple local file explorer for ~/.claude/

Serves a browser UI at http://localhost:8777 that lets you navigate the
directory tree and view file contents. Read-only. Confined to ROOT.
"""
import json
import os
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
           "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
           "connect-src 'self'; base-uri 'none'; form-action 'none'")

    def _send_file(self, path, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", self.CSP)
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
    print("Claude Explorer serving %s" % ROOT)
    print("Open %s   (press Ctrl-C to stop)" % url)
    if os.environ.get("CX_NO_OPEN") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

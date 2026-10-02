#!/usr/bin/env python3
"""Polaris: one local window where your AI agents hand work back to you.

    python3 -m polaris                  # a native window (pywebview) when installed, else the browser
    python3 -m polaris --browser        # the browser even when pywebview is there
    python3 -m polaris --no-window      # serve only and print the URL
    python3 -m polaris --dump           # print the /api/desk payload once, no server
    python3 -m polaris --new            # a second server even when one is running

Local only: the server binds 127.0.0.1, refuses any Host header that is not 127.0.0.1 or
localhost (so a web page cannot reach it by DNS rebinding), and every /api/ route except
/api/health needs the token minted at each start, in the X-Polaris-Token header. The page is
handed the token in the URL fragment (#t=...), which browsers never send to a server, and it
keeps it in sessionStorage. The token is also written to $POLARIS_HOME/state/token with mode
0600, so a second launch can open a window on the running desk, and removed at shutdown.
What this does not stop: any process running as your own user can read that file, as it can
read everything else of yours.

One desk at a time: the running server records its pid and URL in $POLARIS_HOME/state/desk.json.
A second start asks that URL for /api/health and, when the pid matches, opens a window on the
running desk instead of starting another.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import attention, config, pins, questions, run_these, sources, terminal

WEB = Path(__file__).resolve().parents[1] / "web"
GOTO_TTL_S = 600
SCREENS = ("home", "questions", "answers", "run", "terminal")
CTYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".json": "application/json", ".map": "application/json"}


def runfile() -> Path:
    return config.state_file("desk.json")


def tokenfile() -> Path:
    return config.state_file("token")


def write_token(token: str) -> None:
    p = tokenfile()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    try:
        p.unlink()  # a stale file may carry looser permissions: start fresh
    except FileNotFoundError:
        pass
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)


def read_token() -> str:
    try:
        return tokenfile().read_text().strip()
    except OSError:
        return ""


def page_url(base: str, token: str, screen: str = "home") -> str:
    """The URL a window opens: the token rides in the fragment, which never reaches a server log."""
    return f"{base}#t={token}/{screen}"


def gotofile() -> Path:
    return config.state_file("goto.json")


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def goto_request() -> dict:
    """A pending "show this screen" from `python3 -m polaris.open`, or {}."""
    g = _load(gotofile(), {})
    if not isinstance(g, dict) or g.get("seen") or g.get("screen") not in SCREENS:
        return {}
    try:
        age = time.time() - dt.datetime.fromisoformat(g["at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return {}
    return g if age < GOTO_TTL_S else {}


def goto_seen(gid: str) -> bool:
    g = _load(gotofile(), {})
    if g.get("id") != gid:
        return False
    g["seen"] = config.now().isoformat(timespec="seconds")
    gotofile().write_text(json.dumps(g))
    return True


def commands() -> list:
    rows = run_these.open_commands(config.sub("commands"))
    out = []
    for r in rows:
        out.append({k: r.get(k, "") for k in ("id", "title", "why", "from", "created", "note", "expect", "command",
                                               "check", "checked_at", "tested")}
                   | {"verdict": run_these.verdict_kind(r.get("check", ""))})
    return out


def close_command(action: str, cid: str, result: str = "") -> dict:
    if action not in ("done", "drop"):
        raise ValueError("action is done or drop")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cid or ""):
        raise ValueError("bad id")
    store = config.sub("commands")
    path = store / f"{cid}.md"
    if not path.is_file():
        raise KeyError(f"no command {cid}")
    rec = run_these.read_command(path)
    if rec.get("status") != "open":
        raise ValueError(f"{cid} is already {rec.get('status')}")
    if action == "done" and run_these.verdict_kind(rec.get("check", "")) == "stale":
        raise ValueError("a stale command can only be dropped")
    run_these.set_fields(path, {"status": "done" if action == "done" else "dropped",
                                "done_at": run_these._now(), "result": result or ("done in Polaris" if action == "done" else rec.get("check") or "dropped in Polaris")})
    return {"message": f"{cid} {'done' if action == 'done' else 'dropped'}", "id": cid}


class Desk:
    """Builds the one payload the page polls every 3 seconds, cached for two."""

    def __init__(self, src=None):
        self.src = src or sources.load()
        self._cache = (0.0, None)
        self._lock = threading.Lock()

    def payload(self) -> dict:
        with self._lock:
            at, doc = self._cache
            if doc is not None and time.time() - at < 2.0:
                return doc
            doc = self.build()
            self._cache = (time.time(), doc)
            return doc

    def invalidate(self) -> None:
        with self._lock:
            self._cache = (0.0, None)

    def build(self) -> dict:
        now = config.now()
        home = config.home()
        errors = []
        try:
            status = self.src.status()
        except Exception as e:  # noqa: BLE001  a broken source is a line, not a dead desk
            status = {"agents": {"running": [], "finished": []}, "jobs": [], "headline": ""}
            errors.append(f"source status: {type(e).__name__}: {e}")
        qs, later, qerr = questions.items(home, now, self.src)
        errors += qerr
        try:
            cmds = commands()
        except Exception as e:  # noqa: BLE001
            cmds = []
            errors.append(f"run these: {type(e).__name__}: {e}")
        jobs = status.get("jobs") or []
        failed = [f"{now.date()}|{j.get('label', '')}" for j in jobs if j.get("status") in ("failed", "died")]
        ag = status.get("agents") or {}
        attn = attention.compute({"running": len(ag.get("running") or []), "finished": ag.get("finished") or [],
                                  "questions": [q["id"] for q in qs], "failed_jobs": failed,
                                  "run_these": [c["id"] for c in cmds]}, now)
        running = len(ag.get("running") or [])
        state = "error" if attn.get("critical") or failed else "working" if running else "idle"
        return {"ok": True, "now": now.isoformat(timespec="seconds"), "status": status, "state": state,
                "counts": {"questions": len(qs), "later": later, "run_these": len(cmds), "running": running,
                           "failed_jobs": len(failed), "pins": len(pins.unread(home))},
                "top_question": qs[0] if qs else None, "run_these": cmds, "attention": attn,
                "goto": goto_request(), "errors": errors, "source": getattr(self.src, "name", "custom"),
                "example": bool(status.get("example"))}


def make_handler(desk: Desk, token: str):
    page_cache: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "polaris"

        def log_message(self, *a):  # quiet: the terminal it runs in is not a log
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Polaris-Token") or "", token)

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self, cap: int = 1 << 20) -> dict:
            n = min(int(self.headers.get("Content-Length") or 0), cap)
            doc = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(doc, dict):
                raise ValueError("body is a JSON object")
            return doc

        def _static(self, base: Path, rel: str) -> None:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", rel) or ".." in rel:
                return self._json(404, {"ok": False, "error": "no such file"})
            f = (base / rel).resolve()
            if base.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"ok": False, "error": "no such file"})
            return self._send(200, f.read_bytes(), CTYPES.get(f.suffix, "application/octet-stream"), cache=base.name == "vendor")

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "host"})
            path, _, qs = self.path.partition("?")
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}
            if path in ("/", "/index.html"):
                f = WEB / "index.html"
                m = f.stat().st_mtime
                if page_cache.get("m") != m:
                    page_cache.update(m=m, body=f.read_bytes())
                return self._send(200, page_cache["body"], "text/html; charset=utf-8")
            if path == "/api/health":
                return self._json(200, {"ok": True, "pid": os.getpid(), "app": "polaris"})
            if path.startswith("/api/") and not self._token_ok():
                return self._json(403, {"ok": False, "error": "token"})
            if path == "/api/desk":
                return self._json(200, desk.payload())
            if path == "/api/mod/questions":
                return self._json(200, questions.payload(config.home(), config.now(), desk.src))
            if path == "/api/mod/pins":
                u = pins.unread(config.home())
                return self._json(200, {"question": "what did I ask for that is now done", "items": u, "n": len(u)})
            if path == "/api/mod/answers":
                try:
                    rows = desk.src.answers(int(q.get("limit") or 30))
                except Exception as e:  # noqa: BLE001
                    return self._json(200, {"items": [], "n": 0, "errors": [f"{type(e).__name__}: {e}"]})
                return self._json(200, {"question": "what came back from what I asked", "items": rows, "n": len(rows)})
            if path == "/api/mod/run_these":
                rows = commands()
                return self._json(200, {"question": "what must I run by hand", "items": rows, "n": len(rows)})
            if path.startswith("/static/"):
                return self._static(WEB, path[len("/static/"):])
            if path.startswith("/orbs/"):
                return self._static(WEB / "orbs", path[len("/orbs/"):])
            if path.startswith("/vendor/"):
                return self._static(WEB / "vendor", path[len("/vendor/"):])
            if path.startswith("/api/pty"):
                tab = q.get("id") or "session"
                if path == "/api/pty":
                    p = terminal.pty_tab(tab)
                    view = p.view() if p else {"id": tab, "open": False, "exit": None, "ended": "", "cursor": 0}
                    return self._json(200, {"ok": True, **view, "tabs": terminal.pty_tabs(),
                                            "tmux_available": bool(shutil.which("tmux")), "shell": terminal.login_shell()})
                if path == "/api/pty/read":
                    try:
                        cursor, wait = int(q.get("cursor") or 0), min(float(q.get("wait") or 25), 30.0)
                    except ValueError:
                        return self._json(400, {"ok": False, "error": "cursor and wait are numbers"})
                    p = terminal.pty_tab(tab)
                    if p is None:
                        return self._json(404, {"ok": False, "error": f"no such terminal tab {tab!r}"})
                    return self._json(200, p.read(cursor, wait))
                if path == "/api/pty/reads":
                    try:
                        cursors = {t: int(c) for t, _, c in (x.partition(":") for x in (q.get("c") or "").split(",") if x)}
                        wait = min(float(q.get("wait") or 25), 30.0)
                    except ValueError:
                        return self._json(400, {"ok": False, "error": "c is tab:cursor,... and wait a number"})
                    return self._json(200, terminal.pty_reads(cursors, wait))
            return self._json(404, {"ok": False, "error": "no such route"})

        def _pty(self, op: str, body: dict) -> None:
            tab = str(body.get("id") or "session")
            p = terminal.pty_tab(tab, create=(op == "open"))
            if p is None:
                if not terminal.PTY_TAB.fullmatch(tab):
                    return self._json(400, {"ok": False, "error": f"no such terminal tab {tab!r}"})
                if op == "open":
                    return self._json(429, {"ok": False, "error": f"{terminal.PTY_MAX} terminals are open already: close one first"})
                return self._json(409, {"ok": False, "error": "this terminal is not open"})
            if op == "open":
                out = p.open(int(body.get("cols") or 120), int(body.get("rows") or 36))
            elif op == "write":
                out = p.write(base64.b64decode(str(body.get("data") or "")))
            elif op == "resize":
                out = p.resize(int(body.get("cols") or 120), int(body.get("rows") or 36))
            else:
                out = p.close()
            return self._json(out.pop("code", 200) if not out.get("ok") else 200, out)

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "host"})
            routes = ("/api/send", "/api/do", "/api/mod/questions", "/api/mod/pins") + terminal.PTY_ROUTES
            if self.path not in routes:
                return self._json(404, {"ok": False, "error": "no such route"})
            if not self._token_ok():
                return self._json(403, {"ok": False, "error": "token"})
            try:
                body = self._body()
            except ValueError:  # a bad UTF-8 body is a ValueError too
                return self._json(400, {"ok": False, "error": "bad json"})
            if self.path in terminal.PTY_ROUTES:
                return self._pty(self.path.rsplit("/", 1)[1], body)
            now = config.now()
            try:
                if self.path == "/api/mod/questions":
                    out = questions.act(config.home(), now, body, desk.src)
                elif self.path == "/api/mod/pins":
                    op = body.get("op")
                    if op == "dismiss_all":
                        n = sum(1 for p in pins.unread(config.home()) if pins.dismiss(p["id"], config.home()))
                        out = {"message": f"{n} pins dismissed", "n": n}
                    elif op == "dismiss":
                        if not pins.dismiss(str(body.get("id") or ""), config.home()):
                            raise KeyError("no such pin")
                        out = {"message": "", "left": len(pins.unread(config.home()))}
                    else:
                        raise ValueError("op is dismiss or dismiss_all")
                elif self.path == "/api/send":
                    out = send(str(body.get("text") or ""))
                else:
                    out = do(body)
            except KeyError as e:
                return self._json(404, {"ok": False, "error": str(e).strip("'")})
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            desk.invalidate()
            return self._json(200, {"ok": True, **out})

    return H


def do(body: dict) -> dict:
    """The page's small actions: a Run these Done or Drop, an attention acknowledgement, a goto seen."""
    if "run_these" in body:
        rb = body["run_these"] or {}
        return close_command(str(rb.get("action") or ""), str(rb.get("id") or ""), str(rb.get("result") or ""))
    if body.get("attention_ack"):
        attention.acknowledge(str(body.get("key") or ""), config.now())
        return {"message": "acknowledged"}
    if body.get("goto_seen"):
        return {"message": "seen" if goto_seen(str(body["goto_seen"])) else "no such goto"}
    raise ValueError("nothing to do")


def send(text: str) -> dict:
    """The input line: one JSON line appended to $POLARIS_HOME/inbox.jsonl for your session to read."""
    text = text.strip()
    if not text:
        raise ValueError("nothing to send")
    if len(text) > 8000:
        raise ValueError("too long")
    line = {"ts": config.now().isoformat(timespec="seconds"), "text": text, "from": "polaris"}
    p = config.home() / "inbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return {"line": f"sent {len(text)} characters to the inbox"}


def checker_loop(every_s: int) -> None:
    """Run `run_these check --if-due` every `every_s` seconds: it acts when the last check is
    10 minutes old, or sooner when the repo's HEAD moved and touched a file a command names."""
    while True:
        try:
            subprocess.run([sys.executable, "-m", "polaris.run_these", "--store", str(config.sub("commands")),
                            "--state", str(config.state_file("run-these-check.json")), "check", "--if-due"],
                           cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(every_s)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int, token: str | None = None, desk: Desk | None = None):
    """Start the server in a thread; return (server, url, token)."""
    desk = desk or Desk()
    token = token or secrets.token_urlsafe(24)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(desk, token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="polaris").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/", token


def open_window(url: str, browser: bool) -> bool:
    """A native pywebview window when it imports (blocks until closed), else the browser."""
    if not browser:
        try:
            import webview  # type: ignore
            webview.create_window("Polaris", url, width=1500, height=940, min_size=(1000, 680), background_color="#0A0C0D")
            webview.start()
            return True
        except Exception as e:  # noqa: BLE001  any failure falls back to the browser
            print(f"polaris: no native window ({type(e).__name__}: {e}); opening the browser", file=sys.stderr)
    webbrowser.open(url)
    return False


def running_url() -> str:
    """The URL of a desk already serving, or ''. Checked by asking it, not by trusting the pid."""
    try:
        rec = json.loads(runfile().read_text())
        with urllib.request.urlopen(rec["url"] + "api/health", timeout=2) as r:
            if rec.get("pid") and json.loads(r.read()).get("pid") == rec.get("pid"):
                return rec["url"]
    except Exception:  # noqa: BLE001  no file, dead server, another program on the port
        pass
    return ""


def focus_running() -> bool:
    """Bring the running desk's window to the front (macOS, through System Events)."""
    try:
        pid = int(json.loads(runfile().read_text()).get("pid") or 0)
    except (OSError, ValueError):
        return False
    if not pid or sys.platform != "darwin":
        return False
    script = f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def record(url: str, stopped: bool = False) -> None:
    stamp = config.now().isoformat(timespec="seconds")
    try:
        p = runfile()
        p.parent.mkdir(parents=True, exist_ok=True)
        if stopped:
            rec = json.loads(p.read_text())
            if rec.get("pid") != os.getpid():
                return
            rec.update(pid=None, stopped=stamp)
        else:
            rec = {"pid": os.getpid(), "url": url, "started": stamp}
        p.write_text(json.dumps(rec))
    except (OSError, ValueError):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polaris", description="One local window where your agents hand work back to you.")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-window", action="store_true", help="serve only and print the URL")
    ap.add_argument("--browser", action="store_true", help="the default browser, never a native window")
    ap.add_argument("--dump", action="store_true", help="print the payload once and exit")
    ap.add_argument("--new", action="store_true", help="start a second desk even when one is serving")
    a = ap.parse_args(argv)
    if a.dump:
        print(json.dumps(Desk().payload(), indent=1, ensure_ascii=False, default=str))
        return 0
    if not a.new and not a.port:
        url = running_url()
        if url:
            print(f"polaris: already serving at {url}", flush=True)
            if not a.no_window and not focus_running():
                open_window(page_url(url, read_token()), a.browser)
            return 0
    srv, url, token = serve(a.port or free_port())
    write_token(token)
    record(url)
    every = int(os.environ.get("POLARIS_CHECK_EVERY", "60") or 0)
    if every > 0:
        threading.Thread(target=checker_loop, args=(every,), daemon=True, name="polaris-checker").start()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"polaris: {url} (open {page_url(url, '<token in state/token>')})", flush=True)
    try:
        if a.no_window:
            threading.Event().wait()
        elif open_window(page_url(url, token), a.browser):
            return 0
        else:
            print("polaris: serving until ctrl-c", flush=True)
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        record(url, stopped=True)
        if read_token() == token:
            tokenfile().unlink(missing_ok=True)
        for p in list(terminal.PTYS.values()):
            p.close()
        srv.shutdown()
    return 0

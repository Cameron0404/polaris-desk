#!/usr/bin/env python3
"""server_selftest.py - the server end to end over a scratch POLARIS_HOME, never your real one.

    python3 tests/server_selftest.py

It starts the server on a spare port with the example data, fetches every route and checks the
JSON shape, then the safety rules: a write without the token, or with a Host that is not local,
is refused, and a terminal read needs the token too. It drives the terminal routes with /bin/cat
in the pty (bytes round-trip, resize, close), then a real shell under tmux with a scratch session
name when tmux is installed (skipped cleanly when it is not). It answers a question and checks the
follow-up was queued, dismisses a pin, closes and refuses Run these commands, and builds the Mac
app into a temporary folder when sips and iconutil exist (macOS only, skipped elsewhere).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
TMP = Path(tempfile.mkdtemp(prefix="polaris-test-"))
os.environ["POLARIS_HOME"] = str(TMP / "home")
os.environ["POLARIS_REPO"] = str(TMP)
os.environ.pop("POLARIS_SOURCES", None)
os.environ.pop("POLARIS_QUEUE_CMD", None)
shutil.copytree(REPO / "examples" / "home", TMP / "home")

from polaris import server, terminal  # noqa: E402

FAILS: list = []
SKIPS: list = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f": {str(detail)[:300]}"))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print(f"  skip  {name}: {why}")
    SKIPS.append(name)


def main() -> int:
    srv, url, token = server.serve(0, token="test-token-123")
    port = srv.server_address[1]

    def req(method, path, body=None, tok=True, host="127.0.0.1"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
        h = {"Host": f"{host}:{port}", "Content-Type": "application/json"}
        if tok:
            h["X-Polaris-Token"] = token if tok is True else tok
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    try:
        st, page = req("GET", "/", tok=False)
        check("the page is served without the token in it", st == 200 and b"test-token-123" not in page)
        st, _ = req("GET", "/api/desk", tok=False)
        check("a read of /api/desk without the token is refused", st == 403, st)
        for mod in ("questions", "pins", "answers", "run_these"):
            st, _ = req("GET", f"/api/mod/{mod}", tok=False)
            check(f"a read of /api/mod/{mod} without the token is refused", st == 403, st)
        server.write_token(token)
        mode = server.tokenfile().stat().st_mode & 0o777
        check("the token file is readable by its owner only", mode == 0o600 and server.read_token() == token, oct(mode))
        check("the page URL carries the token in the fragment",
              server.page_url(url, token, "run") == url + "#t=test-token-123/run")
        st, h = req("GET", "/api/health", tok=False)
        check("health names the pid", st == 200 and h.get("pid") == os.getpid())
        st, d = req("GET", "/api/desk")
        check("desk payload has its keys", st == 200 and {"status", "counts", "attention", "run_these", "state"} <= set(d), d)
        check("the example data is read", d["counts"]["questions"] == 3 and d["counts"]["run_these"] == 2 and d["example"], d["counts"])
        check("a failed job turns the state to error", d["state"] == "error", d["state"])
        for mod in ("questions", "pins", "answers", "run_these"):
            st, m = req("GET", f"/api/mod/{mod}")
            check(f"/api/mod/{mod} answers", st == 200 and "items" in m, m)
        for p in ("/static/app.js", "/static/style.css", "/orbs/orb_core.js", "/orbs/neural.js"):
            st, _ = req("GET", p, tok=False)
            check(f"{p} is served", st == 200)
        st, _ = req("GET", "/static/../polaris/config.py", tok=False)
        check("a path outside web/ is refused", st == 404, st)

        # ---------------------------------------------------------------- the safety rules
        st, _ = req("POST", "/api/send", {"text": "hi"}, tok=False)
        check("a send without the token is refused", st == 403, st)
        st, _ = req("POST", "/api/send", {"text": "hi"}, tok="wrong")
        check("a send with a wrong token is refused", st == 403, st)
        st, _ = req("POST", "/api/send", {"text": "hi"}, host="evil.example")
        check("a send from another Host is refused", st == 403, st)
        st, _ = req("GET", "/api/desk", host="evil.example")
        check("a read from another Host is refused", st == 403, st)
        st, _ = req("GET", "/api/pty?id=shell-1", tok=False)
        check("a terminal read without the token is refused", st == 403, st)
        inbox = TMP / "home" / "inbox.jsonl"
        st, r = req("POST", "/api/send", {"text": "hello from the test"})
        line = json.loads(inbox.read_text().splitlines()[-1]) if inbox.exists() else {}
        check("a send with the token writes one inbox line", st == 200 and line.get("text") == "hello from the test", (st, r, line))
        bound = srv.server_address[0]
        check("the server binds 127.0.0.1 only", bound == "127.0.0.1", bound)

        # ---------------------------------------------------------------- questions, pins, run these
        st, r = req("POST", "/api/mod/questions", {"op": "answer", "id": "file:example-blog-post", "answer": "publish"})
        q = list((TMP / "home" / "queue").glob("*.json"))
        check("answering a question queues its follow-up", st == 200 and len(q) == 1 and "queued" in r.get("filed", ""), (st, r))
        st, d = req("GET", "/api/desk")
        check("and it leaves the queue", d["counts"]["questions"] == 2, d["counts"])
        st, r = req("POST", "/api/mod/pins", {"op": "dismiss", "id": "example-welcome"})
        st2, d = req("GET", "/api/desk")
        check("a pin is dismissed", st == 200 and d["counts"]["pins"] == 0, (st, r, d["counts"]))
        subprocess.run([sys.executable, "-m", "polaris.run_these", "--store", str(TMP / "home" / "commands"),
                        "--state", str(TMP / "home" / "state" / "rt.json"), "check"], cwd=str(REPO), capture_output=True)
        st, m = req("GET", "/api/mod/run_these")
        kinds = {c["id"]: c["verdict"] for c in m["items"]}
        check("the checker marks the missing-file command stale", kinds.get("example0002") == "stale", kinds)
        check("and the harmless one ok", kinds.get("example0001") == "ok", kinds)
        st, r = req("POST", "/api/do", {"run_these": {"action": "done", "id": "example0002"}})
        check("Done on a stale command is refused", st == 400, (st, r))
        st, r = req("POST", "/api/do", {"run_these": {"action": "drop", "id": "example0002"}})
        check("Drop on a stale command works", st == 200, (st, r))
        st, r = req("POST", "/api/do", {"run_these": {"action": "done", "id": "../../etc"}})
        check("a bad command id is refused", st == 400, (st, r))

        # ---------------------------------------------------------------- goto and attention
        server.gotofile().parent.mkdir(parents=True, exist_ok=True)
        server.gotofile().write_text(json.dumps({"id": "g1", "screen": "questions", "at": server.config.now().isoformat()}))
        time.sleep(2.1)
        st, d = req("GET", "/api/desk")
        check("a goto request reaches the page", d.get("goto", {}).get("id") == "g1", d.get("goto"))
        st, r = req("POST", "/api/do", {"goto_seen": "g1"})
        check("and the page can stamp it seen", st == 200 and r.get("message") == "seen", r)
        st, r = req("POST", "/api/do", {"attention_ack": True, "key": ""})
        check("attention can be acknowledged", st == 200, r)

        # ---------------------------------------------------------------- terminals
        terminal.PTY_ARGV = ["/bin/cat"]
        st, r = req("POST", "/api/pty/open", {"id": "shell-1", "cols": 80, "rows": 24}, tok=False)
        check("opening a terminal without the token is refused", st == 403, st)
        st, r = req("POST", "/api/pty/open", {"id": "shell-1", "cols": 80, "rows": 24})
        check("a terminal opens", st == 200 and r.get("open"), r)
        st, r = req("POST", "/api/pty/open", {"id": "shell-1", "cols": 80, "rows": 24})
        check("a second open of the same tab answers 409", st == 409, st)
        req("POST", "/api/pty/write", {"id": "shell-1", "data": base64.b64encode(b"round-trip-ok\n").decode()})
        got, cur = b"", 0
        for _ in range(20):
            st, r = req("GET", f"/api/pty/reads?c=shell-1:{cur}&wait=2")
            c = r["tabs"]["shell-1"]
            got += base64.b64decode(c["data"])
            cur = c["cursor"]
            if b"round-trip-ok" in got:
                break
        check("bytes round-trip through the pty", b"round-trip-ok" in got, got[:200])
        st, r = req("POST", "/api/pty/resize", {"id": "shell-1", "cols": 100, "rows": 30})
        check("resize answers", st == 200 and r.get("cols") == 100, r)
        st, r = req("POST", "/api/pty/close", {"id": "shell-1"})
        check("close ends it", st == 200 and r.get("open") is False, r)
        st, r = req("POST", "/api/pty/open", {"id": "nope"})
        check("an unknown tab id is refused", st == 400, st)
        terminal.PTY_ARGV = None

        tmux = shutil.which("tmux")
        if not tmux:
            skip("a real shell under tmux", "tmux is not installed")
        else:
            os.environ["POLARIS_TMUX_PREFIX"] = f"polaris-selftest-{os.getpid()}"
            try:
                st, r = req("POST", "/api/pty/open", {"id": "shell-2", "cols": 90, "rows": 25})
                check("a shell tab opens under tmux", st == 200 and r.get("tmux"), r)
                time.sleep(1.0)
                req("POST", "/api/pty/write", {"id": "shell-2", "data": base64.b64encode(b"echo polaris-$((40+2))-ok\r").decode()})
                got, cur = b"", 0
                for _ in range(30):
                    st, r = req("GET", f"/api/pty/reads?c=shell-2:{cur}&wait=2")
                    c = r["tabs"]["shell-2"]
                    got += base64.b64decode(c["data"])
                    cur = c["cursor"]
                    if b"polaris-42-ok" in got:
                        break
                check("the shell runs a command and the output comes back", b"polaris-42-ok" in got, got[-300:])
                st, r = req("POST", "/api/pty/close", {"id": "shell-2"})
                check("closing a tmux tab is a detach", st == 200 and r.get("tmux") and "detached" in (r.get("ended") or ""), r)
            finally:
                subprocess.run([tmux, "kill-session", "-t", "=" + terminal.tmux_name("shell-2")], capture_output=True)
                os.environ.pop("POLARIS_TMUX_PREFIX", None)
    finally:
        srv.shutdown()

    # ---------------------------------------------------------------- the Mac app
    if sys.platform != "darwin" or not (shutil.which("sips") and shutil.which("iconutil")):
        skip("Polaris.app builds", "needs macOS with sips and iconutil")
    else:
        app = TMP / "Polaris.app"
        r = subprocess.run(["bash", str(REPO / "scripts" / "make_polaris_app.sh"), str(app)], capture_output=True, text=True)
        exe = app / "Contents" / "MacOS" / "Polaris"
        ok = r.returncode == 0 and exe.is_file() and (app / "Contents" / "Resources" / "Polaris.icns").is_file()
        check("Polaris.app builds with its icon", ok, r.stderr[-300:])
        check("the app runs this checkout's bin/polaris", ok and str(REPO / "bin" / "polaris") in exe.read_text())

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"server: {'all passed' if not FAILS else str(len(FAILS)) + ' failed'}" + (f", {len(SKIPS)} skipped" if SKIPS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())

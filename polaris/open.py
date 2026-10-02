#!/usr/bin/env python3
"""Show a Polaris screen from a script, a hook or a scheduled job.

    python3 -m polaris.open questions          # home | questions | answers | run | terminal
    python3 -m polaris.open run --dry-run      # say what it would do, open nothing

1. It writes $POLARIS_HOME/state/goto.json ({id, screen, at}). The page reads it on every
   /api/desk poll (every 3 s), switches to the screen once, and stamps `seen`. `seen` is the
   only proof taken that the screen was shown.
2. A desk already serving: its window is brought to the front and the page has 12 s to obey.
   No page obeys (the browser tab was closed): the desk is opened at that screen in the browser,
   with the token from state/token in the URL fragment.
3. No desk: Polaris.app is opened (POLARIS_APP_PATH, default ~/Applications/Polaris.app), else
   `python3 -m polaris` is started detached, and the new page has 45 s to obey.

The outcome is printed as one line and the exit code is 0 only when the page confirmed it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from . import server

WAIT_RUNNING_S = 12
WAIT_LAUNCH_S = 45


def ask(screen: str) -> str:
    gid = uuid.uuid4().hex[:12]
    p = server.gotofile()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": gid, "screen": screen, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}))
    return gid


def seen(gid: str) -> bool:
    try:
        g = json.loads(server.gotofile().read_text())
    except (OSError, ValueError):
        return False
    return g.get("id") == gid and bool(g.get("seen"))


def wait_seen(gid: str, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if seen(gid):
            return True
        time.sleep(0.5)
    return False


def launch() -> bool:
    app = Path(os.environ.get("POLARIS_APP_PATH") or Path.home() / "Applications" / "Polaris.app")
    if sys.platform == "darwin" and app.exists():
        return subprocess.run(["open", str(app)], capture_output=True).returncode == 0
    subprocess.Popen([sys.executable, "-m", "polaris"], cwd=str(Path(__file__).resolve().parents[1]),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def open_screen(screen: str, dry: bool = False) -> str:
    url = server.running_url()
    if dry:
        return f"would show {screen} on " + (f"the running desk at {url}" if url else "a new desk")
    gid = ask(screen)
    if url:
        server.focus_running()
        if wait_seen(gid, WAIT_RUNNING_S):
            return f"shown: {screen} on the running desk"
        webbrowser.open(server.page_url(url, server.read_token(), screen))
        return f"shown: {screen} in the browser" if wait_seen(gid, WAIT_RUNNING_S) else "not confirmed: no page obeyed"
    if not launch():
        return "not shown: Polaris could not be started"
    return f"shown: {screen} on a new desk" if wait_seen(gid, WAIT_LAUNCH_S) else "not confirmed: the new desk did not obey in time"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polaris.open", description="Show a Polaris screen.")
    ap.add_argument("screen", choices=server.SCREENS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    out = open_screen(a.screen, a.dry_run)
    print(out)
    return 0 if out.startswith(("shown", "would")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

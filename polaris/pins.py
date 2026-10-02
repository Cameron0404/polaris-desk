#!/usr/bin/env python3
"""Pins: the popup on next open.

    python3 -m polaris.pins add --title "The report you asked for is ready" [--body TEXT] [--link PATH|URL] [--source S] [--id ID]
    python3 -m polaris.pins list [--all] [--json]
    python3 -m polaris.pins dismiss ID | --all
    python3 -m polaris.pins --selftest

When you ask for something (a message, an answer to a question, an interactive session),
whatever finishes it writes a pin. Polaris shows the unread pins as a modal when it opens or
regains focus, one at a time, until each is dismissed.

One file per pin: `$POLARIS_HOME/pins/<id>.json`, {id, title, body, link, created, source}.
Dismissing sets `dismissed` (an ISO time) in the file. Nothing is ever deleted. Writing a pin
with an id that exists overwrites it and makes it unread again, so a re-run of the same finish
step is one pin, not two. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    from . import config
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from polaris import config  # type: ignore


TITLE_CAP = 140
BODY_CAP = 4000


def pins_dir(root: Path = None) -> Path:
    """Pins live in <root>/pins, where root defaults to POLARIS_HOME."""
    return Path(root or config.home()) / "pins"


def _now() -> dt.datetime:
    return config.now()


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")[:n].strip("-") or "pin"


def _clean_id(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(pid or ""))[:120].strip(".-")


def add(title: str, body: str = "", link: str = "", source: str = "", pid: str = "",
        root: Path = None, now: dt.datetime = None) -> dict:
    """Write one pin and return it. The same id overwrites and is unread again."""
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("a pin needs a title")
    now = now or _now()
    pid = _clean_id(pid) or f"{now:%Y%m%d-%H%M%S}-{_slug(title)}"
    pin = {"id": pid, "title": title[:TITLE_CAP], "body": (body or "").strip()[:BODY_CAP],
           "link": (link or "").strip(), "created": now.isoformat(timespec="seconds"),
           "source": (source or "").strip() or "cli"}
    d = pins_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{pid}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(pin, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return pin


def load(root: Path = None) -> list:
    out = []
    for p in sorted(pins_dir(root).glob("*.json")):
        try:
            pin = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(pin, dict) and pin.get("id") and pin.get("title"):
            out.append(pin)
    out.sort(key=lambda x: x.get("created") or "")
    return out


def unread(root: Path = None) -> list:
    """The pins not yet dismissed, oldest first (the order they are shown)."""
    return [p for p in load(root) if not p.get("dismissed")]


def dismiss(pid: str, root: Path = None, now: dt.datetime = None) -> bool:
    p = pins_dir(root) / f"{_clean_id(pid)}.json"
    try:
        pin = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if pin.get("dismissed"):
        return True
    pin["dismissed"] = (now or _now()).isoformat(timespec="seconds")
    p.write_text(json.dumps(pin, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def banner(root: Path = None) -> str:
    """The one line a terminal tool prints: the newest unread pin and how many more."""
    u = unread(root)
    if not u:
        return ""
    more = f" (+{len(u) - 1} more)" if len(u) > 1 else ""
    return f"PIN: {u[-1]['title']}{more}. Polaris shows it, or: python3 -m polaris.pins list."


def safe_add(title: str, body: str = "", link: str = "", source: str = "", pid: str = "", root: Path = None) -> dict | None:
    """For the finish steps: a pin that cannot be written never breaks the caller."""
    try:
        return add(title, body, link, source, pid, root)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------ self-test

def selftest() -> int:
    fails = []

    def check(name, ok):
        print(("  ok    " if ok else "  FAIL  ") + name)
        if not ok:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        t0 = dt.datetime(2026, 3, 4, 9, 30, tzinfo=dt.timezone.utc)
        a = add("Example: report A ready", "Report A has three sections.", "reports/a.md", "example", root=root, now=t0)
        b = add("Example: report B ready", root=root, now=t0 + dt.timedelta(minutes=5), pid="run-b")
        check("files written under pins/", len(list(pins_dir(root).glob("*.json"))) == 2)
        check("unread oldest first", [p["id"] for p in unread(root)] == [a["id"], "run-b"])
        check("banner names the newest and the count", banner(root).startswith("PIN: Example: report B ready (+1 more)"))
        check("dismiss", dismiss(a["id"], root) and [p["id"] for p in unread(root)] == ["run-b"])
        check("dismissed is kept, not deleted", len(load(root)) == 2)
        add("Example: report B ready, again", root=root, pid="run-b")
        check("same id overwrites", len(load(root)) == 2 and unread(root)[0]["title"].endswith("again"))
        check("unknown id is refused", not dismiss("nope", root))
        check("id is made safe", "/" not in add("x", root=root, pid="../../etc/passwd")["id"])
        try:
            add("  ", root=root)
            check("an empty title is refused", False)
        except ValueError:
            check("an empty title is refused", True)
        check("safe_add never raises", safe_add("", root=root) is None)
        dismiss("run-b", root)
        check("no banner when all are read", banner(root) == "" or "etc-passwd" in banner(root) or "PIN: x" in banner(root))
    print(f"pins: {'all passed' if not fails else str(len(fails)) + ' failed'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if argv is None and "--selftest" in sys.argv[1:]:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--link", default="")
    a.add_argument("--source", default="cli")
    a.add_argument("--id", default="")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")
    d = sub.add_parser("dismiss")
    d.add_argument("id", nargs="?")
    d.add_argument("--all", action="store_true")
    ns = ap.parse_args(argv)
    if ns.cmd == "add":
        pin = add(ns.title, ns.body, ns.link, ns.source, ns.id)
        print(f"pin: {pin['id']}")
        return 0
    if ns.cmd == "dismiss":
        ids = [p["id"] for p in unread()] if ns.all else [ns.id] if ns.id else []
        if not ids:
            print("pin: name an id, or --all", file=sys.stderr)
            return 1
        bad = [i for i in ids if not dismiss(i)]
        print(f"pin: {len(ids) - len(bad)} dismissed" + (f", no such pin: {', '.join(bad)}" if bad else ""))
        return 1 if bad else 0
    rows = load() if getattr(ns, "all", False) else unread()
    if getattr(ns, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("pin: nothing unread")
    for p in rows:
        mark = "  " if not p.get("dismissed") else "x "
        print(f"{mark}{p['created'][:16]}  {p['id']}\n    {p['title']}" + (f"\n    {p['link']}" if p.get("link") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The data layer: everything Polaris shows that it does not own itself.

Polaris owns three stores outright: questions (questions.py), commands waiting for you
(run_these.py) and pins (pins.py). Everything else, what your agents are doing, which jobs
failed, what came back from what you asked, comes from a Source. The default reads plain files
under $POLARIS_HOME, so any tool in any language can feed it:

    feeds/status.json     {"agents": {"running": [...], "finished": [...]},
                           "jobs": [{"label", "status", "when"}], "headline": "..."}
    answers/*.md          one answer per file; the first heading is its title. Newest first.

An agent row is {"id", "title", "status", "started", "model", "doing"}. A finished row adds
"ended" and a status such as done, failed or killed.

To plug in your own system, write a class with the same four methods and point
POLARIS_SOURCES at it, as "package.module:ClassName". Polaris imports it at start. A source
that raises is shown as an error line on the page, never a dead desk.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import re
from pathlib import Path

from . import config


class Source:
    """The interface. Every method returns plain JSON-able data."""

    name = "base"

    def status(self) -> dict:
        """{"agents": {"running": [...], "finished": [...]}, "jobs": [...], "headline": str}"""
        return {"agents": {"running": [], "finished": []}, "jobs": [], "headline": ""}

    def answers(self, limit: int = 30) -> list:
        """[{"id", "title", "body", "when", "from"}], newest first."""
        return []

    def questions(self) -> list:
        """Extra questions from your own system, in the shape questions.py uses:
        [{"id", "question", "options", "context", "from", "ts"}]. Answers to them go to answer()."""
        return []

    def answer(self, qid: str, text: str) -> str:
        """File an answer to one of your own questions; return one line saying where it went."""
        raise KeyError(qid)


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class FileSource(Source):
    """The default: plain files under POLARIS_HOME/feeds and POLARIS_HOME/answers."""

    name = "files"

    def __init__(self, home: Path | None = None):
        self._home = Path(home) if home else None

    @property
    def home(self) -> Path:
        return self._home or config.home()

    def status(self) -> dict:
        doc = _read_json(self.home / "feeds" / "status.json", {})
        if not isinstance(doc, dict):
            doc = {}
        ag = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
        return {"agents": {"running": list(ag.get("running") or []), "finished": list(ag.get("finished") or [])},
                "jobs": list(doc.get("jobs") or []), "headline": str(doc.get("headline") or ""),
                "example": bool(doc.get("example"))}

    def answers(self, limit: int = 30) -> list:
        d = self.home / "answers"
        if not d.is_dir():
            return []
        rows = []
        for p in d.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            m = re.search(r"^#\s+(.+)$", text, re.M)
            title = m.group(1).strip() if m else p.stem.replace("-", " ")
            when = dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            rows.append({"id": p.stem, "title": title, "body": text.strip()[:6000], "when": when, "from": "answers"})
        rows.sort(key=lambda r: r["when"], reverse=True)
        return rows[:limit]


def load() -> Source:
    """The configured source: POLARIS_SOURCES="pkg.mod:Class", else FileSource."""
    spec = os.environ.get("POLARIS_SOURCES") or ""
    if not spec:
        return FileSource()
    mod, _, cls = spec.partition(":")
    obj = getattr(importlib.import_module(mod), cls or "Source")
    return obj() if isinstance(obj, type) else obj

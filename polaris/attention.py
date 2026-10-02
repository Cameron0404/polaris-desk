#!/usr/bin/env python3
"""The attention signal: when the page should change its orb so you notice something new.

`compute(snapshot, now)` folds five triggers into one `{on, why, since, key, critical}`:

  agents_done  every agent's running count fell from one or more to zero. Held until
               acknowledged or 30 minutes pass.
  broken       an agent run that died, failed or timed out (not one killed on purpose) that
               was not seen at the last acknowledgement. Critical: the orb turns red.
  questions    a question that was not there, or not seen, at the last acknowledgement.
  failed_job   a scheduled job marked failed that was not seen at the last acknowledgement.
  run_these    a command waiting for you that was not seen at the last acknowledgement.

Each is edge- or seen-set-gated, so a standing backlog never lights the orb by itself: only
something new since you last acknowledged does. On the very first call (no state file) every
seen-set is seeded from what is there now, unlit, so day one does not open with everything "new".

State: $POLARIS_HOME/state/attention.json, written only here.

    python3 -m polaris.attention --selftest
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from . import config
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from polaris import config  # type: ignore

HOLD_AGENTS_DONE_S = 30 * 60
BROKEN = ("dead", "failed", "timed-out", "orphaned", "crashed")   # not "killed": that was on purpose
SETS = ("questions", "failed", "run_these", "broken")


def _file(home: Path | None = None) -> Path:
    return Path(home or config.home()) / "state" / "attention.json"


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _parse(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _load(home=None) -> dict:
    try:
        return json.loads(_file(home).read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict, home=None) -> None:
    p = _file(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"attention.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def compute(snap: dict, now: dt.datetime, home=None) -> dict:
    """snap: {"running": int, "finished": [{"id", "status"}], "questions": [ids],
    "failed_jobs": [keys], "run_these": [ids]}."""
    st = _load(home)
    first = "questions_seen" not in st

    running = int(snap.get("running") or 0)
    if st.get("prev_running", 0) > 0 and running == 0 and st.get("agents_done_since") is None:
        st["agents_done_since"] = _iso(now)
        st["agents_done_n"] = st["prev_running"]
        st["agents_done_acked"] = False
    if running > 0:
        st["agents_done_since"] = None
    st["prev_running"] = running

    current = {
        "questions": {str(x) for x in snap.get("questions") or []},
        "failed": {str(x) for x in snap.get("failed_jobs") or []},
        "run_these": {str(x) for x in snap.get("run_these") or []},
        "broken": {str(r.get("id")) for r in snap.get("finished") or [] if r.get("status") in BROKEN and r.get("id")},
    }
    new = {}
    for k in SETS:
        seen = set(current[k]) if first else set(st.get(f"{k}_seen") or [])
        new[k] = current[k] - seen
        st[f"{k}_seen"] = sorted(seen)
        st[f"{k}_current"] = sorted(current[k])

    reasons = []
    since = _parse(st.get("agents_done_since"))
    if since and not st.get("agents_done_acked"):
        if (now - since).total_seconds() <= HOLD_AGENTS_DONE_S:
            n = st.get("agents_done_n") or 1
            reasons.append(("agents_done", f"All {n} {_plural(n, 'agent is', 'agents are')} done.", since))
        else:
            st["agents_done_since"] = None
    if new["broken"]:
        n = len(new["broken"])
        reasons.append(("broken", f"{n} agent {_plural(n, 'run', 'runs')} broke.", now))
    if new["questions"]:
        n = len(new["questions"])
        reasons.append(("questions", f"{n} new {_plural(n, 'question', 'questions')} for you.", now))
    if new["failed"]:
        n = len(new["failed"])
        reasons.append(("failed_job", f"{n} {_plural(n, 'job', 'jobs')} failed since you last looked.", now))
    if new["run_these"]:
        n = len(new["run_these"])
        reasons.append(("run_these", f"{n} {_plural(n, 'command', 'commands')} waiting for you to run.", now))
    _save(st, home)
    if not reasons:
        return {"on": False, "why": "", "since": "", "key": "", "critical": False}
    key, why, when = reasons[0]
    return {"on": True, "why": why, "since": _iso(when), "key": key, "critical": bool(new["broken"])}


def acknowledge(reason: str, now: dt.datetime, home=None) -> None:
    """Clear one reason (the key the page showed), or everything when reason is empty."""
    st = _load(home)
    reason = reason or ""
    if reason in ("", "agents_done"):
        st["agents_done_acked"] = True
    for key, k in (("questions", "questions"), ("failed_job", "failed"), ("run_these", "run_these"), ("broken", "broken")):
        if reason in ("", key):
            st[f"{k}_seen"] = sorted(set(st.get(f"{k}_seen") or []) | set(st.get(f"{k}_current") or []))
    st["acked_at"] = _iso(now)
    st["acked_reason"] = reason
    _save(st, home)


def selftest() -> int:
    fails = []

    def check(name, ok, detail=""):
        print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f": {detail}"))
        if not ok:
            fails.append(name)

    t = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        a = compute({"running": 2, "questions": ["q1", "q2"], "failed_jobs": ["j1"], "run_these": ["c1"]}, t, h)
        check("first run seeds every seen-set, unlit", not a["on"], a)
        a = compute({"running": 0, "questions": ["q1", "q2"], "failed_jobs": ["j1"], "run_these": ["c1"]}, t, h)
        check("agents falling to zero lights agents_done", a["on"] and a["key"] == "agents_done" and "2 agents" in a["why"], a)
        acknowledge("agents_done", t, h)
        a = compute({"running": 0, "questions": ["q1", "q2"], "failed_jobs": ["j1"], "run_these": ["c1"]}, t, h)
        check("acknowledged, it clears", not a["on"], a)
        a = compute({"running": 0, "questions": ["q1", "q2", "q3"], "failed_jobs": ["j1"], "run_these": ["c1"]}, t, h)
        check("a new question lights it", a["on"] and a["key"] == "questions", a)
        a = compute({"running": 0, "questions": ["q1", "q2", "q3"], "failed_jobs": ["j1"], "run_these": ["c1", "c2"]}, t, h)
        check("the first live reason wins", a["key"] == "questions", a)
        acknowledge("questions", t, h)
        a = compute({"running": 0, "questions": ["q1", "q2", "q3"], "failed_jobs": ["j1"], "run_these": ["c1", "c2"]}, t, h)
        check("then the next reason shows", a["key"] == "run_these" and "1 command" in a["why"], a)
        acknowledge("", t, h)
        a = compute({"running": 0, "questions": ["q1", "q2", "q3"], "failed_jobs": ["j1"], "run_these": ["c1", "c2"]}, t, h)
        check("an empty acknowledgement clears everything", not a["on"], a)
        a = compute({"running": 0, "finished": [{"id": "r1", "status": "failed"}, {"id": "r2", "status": "killed"}],
                     "questions": ["q1", "q2", "q3"], "run_these": ["c1", "c2"]}, t, h)
        check("a broken run is critical, a killed one is not", a["key"] == "broken" and a["critical"] and "1 agent run" in a["why"], a)
        compute({"running": 1}, t, h)
        acknowledge("", t, h)
        compute({"running": 0}, t, h)
        a = compute({"running": 0}, t + dt.timedelta(minutes=31), h)
        check("agents_done lapses after 30 minutes", not a["on"], a)
    print(f"attention: {'all passed' if not fails else str(len(fails)) + ' failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(selftest())
    print(__doc__)

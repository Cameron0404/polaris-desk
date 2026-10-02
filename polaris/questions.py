#!/usr/bin/env python3
"""Questions: one queue of everything waiting on your answer, oldest blocker first.

Any run can ask you something by writing a question file to $POLARIS_HOME/questions/<id>.json:

    {"id": "publish-post", "question": "Publish the draft blog post now?",
     "options": ["publish", "not yet"], "context": "one line of why",
     "details": "optional longer markdown", "source": "writing agent",
     "on_answer": {"if": ["publish"],
                   "queue": {"title": "Publish the blog post", "at": "now",
                             "text": "the brief for the follow-up run", "model": "sonnet"}}}

Answering writes `answer`, `answered_at` and `evidence` into that file. When the question has
`on_answer.queue` and the answer is one of `on_answer.if` (any answer when `if` is absent), the
follow-up is handed to your runner, so a yes launches the work and a no launches nothing:

  - POLARIS_QUEUE_CMD set: that command is run with the entry as JSON on stdin, and its last
    output line is recorded (for example a wrapper around your own queue tool).
  - otherwise the entry is written to $POLARIS_HOME/queue/<time>-<slug>.json for your runner.

Skip sends a question to the back. "Ask me later" hides it until 21:00, or 09:00 tomorrow.
The configured Source (sources.py) can add questions of its own and file their answers.
Queue state (answered, skipped, later) lives in $POLARIS_HOME/state/questions.json.

    python3 -m polaris.questions --selftest
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from . import config, sources
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from polaris import config, sources  # type: ignore

QUESTION = "what is waiting on my answer"
STATE = "questions.json"


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _parse(s):
    try:
        return dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _options(raw) -> list:
    if isinstance(raw, str):
        raw = [x for x in re.split(r"\s*[|/]\s*", raw) if x]
    return [str(x).strip() for x in (raw or []) if str(x).strip()][:9]


def qdir(home: Path) -> Path:
    return home / "questions"


def file_items(home: Path, now: dt.datetime) -> list:
    out = []
    for p in sorted(qdir(home).glob("*.json")):
        q = _load(p, None)
        if not isinstance(q, dict) or not str(q.get("question") or "").strip() or q.get("answered_at"):
            continue
        qid = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(q.get("id") or p.stem))[:120]
        ts = _parse(q.get("created")) or dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        details = "\n\n".join(x for x in (str(q.get("context") or "").strip(), str(q.get("details") or "").strip(),
                                          f"**From:** {q.get('source') or 'a run'}") if x)
        out.append({"id": f"file:{qid}", "kind": "file", "question": q["question"].strip(),
                    "context": str(q.get("context") or "").strip()[:1500], "options": _options(q.get("options")),
                    "from": str(q.get("source") or "a run"), "path": p.name, "on_answer": q.get("on_answer") or None,
                    "details": details, "example": bool(q.get("example")), "ts": _iso(ts)})
    return out


def source_items(src) -> list:
    out = []
    for q in src.questions() or []:
        out.append({"id": f"src:{q['id']}", "kind": "source", "question": str(q.get("question") or ""),
                    "context": str(q.get("context") or ""), "options": _options(q.get("options")),
                    "from": str(q.get("from") or src.name), "details": str(q.get("details") or ""),
                    "ts": str(q.get("ts") or ""), "raw_id": q["id"]})
    return out


def _state(home: Path) -> dict:
    st = _load(home / "state" / STATE, {})
    for k in ("answered", "later", "skipped"):
        st.setdefault(k, {})
    return st


def _later_until(now: dt.datetime) -> dt.datetime:
    t = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if t > now:
        return t
    return (now + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)


def items(home: Path, now: dt.datetime, src=None) -> tuple:
    st = _state(home)
    out, later, errors = [], 0, []
    gathered = []
    try:
        gathered += file_items(home, now)
    except Exception as e:  # noqa: BLE001  one broken source is a line, not an empty screen
        errors.append(f"question files: {type(e).__name__}: {e}")
    if src is not None:
        try:
            gathered += source_items(src)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{getattr(src, 'name', 'source')}: {type(e).__name__}: {e}")
    for it in gathered:
        if it["id"] in st["answered"]:
            continue
        until = _parse(st["later"].get(it["id"]))
        if until and until > now:
            later += 1
            continue
        it["skipped"] = bool(st["skipped"].get(it["id"]))
        out.append(it)
    far = now + dt.timedelta(days=3650)
    out.sort(key=lambda i: (i["skipped"], st["skipped"].get(i["id"], ""), _parse(i.get("ts")) or far))
    return out, later, errors


def payload(home: Path, now: dt.datetime, src=None) -> dict:
    its, later, errors = items(home, now, src)
    return {"question": QUESTION, "items": its, "n": len(its), "later": later, "errors": errors}


def evidence(now: dt.datetime) -> str:
    return f"Owner, in Polaris, {now.strftime('%a %d %b %Y %H:%M')}"


def _queue(home: Path, entry: dict) -> str:
    cmd = os.environ.get("POLARIS_QUEUE_CMD")
    if cmd:
        r = subprocess.run(shlex.split(cmd), input=json.dumps(entry), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return f"not launched: the queue command failed ({(r.stderr or r.stdout).strip()[:200]})"
        last = (r.stdout.strip().splitlines() or [entry["title"]])[-1]
        return "queued: " + last
    d = home / "queue"
    slug = re.sub(r"[^a-z0-9]+", "-", entry["title"].lower()).strip("-")[:40] or "entry"
    p = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}.json"
    _save(p, entry)
    return f"queued: {p.name}"


def _on_answer(home: Path, it: dict, answer: str) -> str:
    oa = it.get("on_answer") or {}
    qe = oa.get("queue") if isinstance(oa, dict) else None
    if not qe:
        return ""
    want = [str(x).strip().lower() for x in (oa.get("if") or [])]
    if want and answer.strip().lower() not in want:
        return f"not launched: the answer is not one of {', '.join(want)}"
    title = str(qe.get("title") or "").strip()
    if not title:
        return "not launched: the question's queue entry has no title"
    # The closing line: a brief that says "stop and write nothing" on one answer can end a run
    # with no record at all, which a supervisor reads as a no-op. Writing what it did is part of the work.
    text = (f"# Request from Polaris: the answer was \"{answer.strip()}\" to: {it['question']}\n\n"
            + str(qe.get("text") or "").strip() + "\n\n"
            "In every case, including a stop with nothing to change, record the answer you read, "
            "what you did and why, before you finish.\n")
    entry = {"title": title, "at": str(qe.get("at") or "now"), "text": text, "model": qe.get("model") or "",
             "file": qe.get("file") or "", "reason": f"polaris, answer to {it['id']}"}
    return _queue(home, entry)


def answer(home: Path, now: dt.datetime, qid: str, text: str, src=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("an empty answer is not an answer")
    its, _, _ = items(home, now, src)
    it = next((i for i in its if i["id"] == qid), None)
    if it is None:
        raise KeyError(f"no open question {qid}")
    st = _state(home)
    if it["kind"] == "file":
        p = qdir(home) / it["path"]
        q = _load(p, {})
        q.update(answer=text, answered_at=_iso(now), evidence=evidence(now))
        launched = _on_answer(home, it, text)
        if launched:
            q["launched"] = launched
        _save(p, q)
        said = "answered in the question file" + (f", {launched}" if launched else "")
    else:
        said = src.answer(it["raw_id"], text) if src is not None else "no source to file it"
    st["answered"][qid] = {"at": _iso(now), "answer": text[:300]}
    st["skipped"].pop(qid, None)
    st["later"].pop(qid, None)
    cut = now - dt.timedelta(days=30)
    st["answered"] = {k: v for k, v in st["answered"].items() if (_parse(v.get("at")) or now) > cut}
    _save(home / "state" / STATE, st)
    return {"message": f"Answered: {said}", "id": qid, "filed": said}


def act(home: Path, now: dt.datetime, body: dict, src=None) -> dict:
    """{op: answer|skip|later|unskip, id, answer}"""
    op, qid = body.get("op"), str(body.get("id") or "")[:200]
    if not qid:
        raise ValueError("name the question's id")
    if op == "answer":
        return answer(home, now, qid, str(body.get("answer") or ""), src)
    st = _state(home)
    if op == "skip":
        st["skipped"][qid] = _iso(now)
        msg = "skipped, it comes back after the others"
    elif op == "later":
        u = _later_until(now)
        st["later"][qid] = _iso(u)
        msg = f"asked again at {u.strftime('%a %H:%M')}"
    elif op == "unskip":
        st["skipped"].pop(qid, None)
        st["later"].pop(qid, None)
        msg = "back in the queue"
    else:
        raise ValueError("op is answer, skip, later or unskip")
    _save(home / "state" / STATE, st)
    return {"message": msg, "id": qid}


def ask(home: Path, question: str, options=None, context: str = "", source: str = "cli", qid: str = "",
        on_answer: dict | None = None) -> Path:
    """Write a question file: what any run calls to ask you something."""
    now = config.now()
    qid = re.sub(r"[^A-Za-z0-9._-]+", "-", qid or f"{now:%Y%m%d-%H%M%S}-{question[:30].lower()}").strip("-")
    doc = {"id": qid, "question": question, "options": list(options or []), "context": context,
           "source": source, "created": _iso(now)}
    if on_answer:
        doc["on_answer"] = on_answer
    p = qdir(home) / f"{qid}.json"
    _save(p, doc)
    return p


# ================================================================================ self-test

def selftest() -> int:
    fails = []

    def check(name, ok, detail=""):
        print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f": {detail}"))
        if not ok:
            fails.append(name)

    tz = dt.timezone.utc
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        os.environ.pop("POLARIS_QUEUE_CMD", None)
        t0 = dt.datetime(2026, 9, 27, 14, 0, tzinfo=tz)
        ask(home, "Example: publish the draft blog post now?", ["publish", "not yet"], "why", "example agent",
            qid="post", on_answer={"if": ["publish"], "queue": {"title": "Publish the blog post", "text": "Publish it."}})
        ask(home, "Example: which colour for the chart?", ["blue", "green"], qid="colour")
        _save(qdir(home) / "old.json", {"id": "old", "question": "Example: oldest?", "created": "2026-09-20T10:00:00+00:00"})
        its, _, errs = items(home, t0)
        check("three open questions, no errors", len(its) == 3 and not errs, (its, errs))
        check("oldest blocker first", its[0]["id"] == "file:old", [i["id"] for i in its])
        r = act(home, t0, {"op": "skip", "id": "file:old"})
        its, _, _ = items(home, t0)
        check("skip sends it to the back", its[-1]["id"] == "file:old" and "skipped" in r["message"])
        act(home, t0, {"op": "later", "id": "file:colour"})
        its, later, _ = items(home, t0)
        check("later hides it until 21:00", later == 1 and all(i["id"] != "file:colour" for i in its))
        its, later, _ = items(home, t0.replace(hour=21, minute=1))
        check("and it is back after 21:00", any(i["id"] == "file:colour" for i in its))
        r = answer(home, t0, "file:post", "not yet")
        check("a no launches nothing", "not launched" in r["filed"] and not (home / "queue").exists(), r)
        q = _load(qdir(home) / "post.json", {})
        check("the answer is written into the file", q.get("answer") == "not yet" and q.get("evidence"))
        ask(home, "Example: send it now?", ["yes", "no"], qid="send2",
            on_answer={"if": ["yes"], "queue": {"title": "Send it", "text": "Do it."}})
        r = answer(home, t0, "file:send2", "yes")
        qs = list((home / "queue").glob("*.json"))
        check("a yes queues the follow-up", len(qs) == 1 and "queued" in r["filed"], r)
        e = _load(qs[0], {}) if qs else {}
        check("the entry carries the answer and the reason", "yes" in e.get("text", "") and e.get("reason") == "polaris, answer to file:send2")
        os.environ["POLARIS_QUEUE_CMD"] = f"{sys.executable} -c \"import sys,json; print('ok '+json.load(sys.stdin)['title'])\""
        ask(home, "Example: again?", ["yes"], qid="send3", on_answer={"queue": {"title": "Again"}})
        r = answer(home, t0, "file:send3", "yes")
        os.environ.pop("POLARIS_QUEUE_CMD", None)
        check("POLARIS_QUEUE_CMD receives the entry on stdin", r["filed"].endswith("queued: ok Again"), r)
        try:
            answer(home, t0, "file:colour", "  ")
            check("an empty answer is refused", False)
        except ValueError:
            check("an empty answer is refused", True)
        try:
            answer(home, t0, "file:nope", "x")
            check("an unknown id is refused", False)
        except KeyError:
            check("an unknown id is refused", True)

        class Src(sources.Source):
            name = "mine"
            got = []

            def questions(self):
                return [{"id": "a1", "question": "Example: from my source?", "options": ["yes"]}]

            def answer(self, qid, text):
                self.got.append((qid, text))
                return "filed by my source"

        s = Src()
        its, _, _ = items(home, t0, s)
        check("a Source adds its own questions", any(i["id"] == "src:a1" for i in its))
        r = answer(home, t0, "src:a1", "yes", s)
        check("and files their answers", s.got == [("a1", "yes")] and "filed by my source" in r["filed"])

        class Broken(sources.Source):
            name = "broken"

            def questions(self):
                raise RuntimeError("boom")

        _, _, errs = items(home, t0, Broken())
        check("a broken source is an error line, not a crash", errs and "broken" in errs[0], errs)
    print(f"questions: {'all passed' if not fails else str(len(fails)) + ' failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(selftest())
    print(__doc__)

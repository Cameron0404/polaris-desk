#!/usr/bin/env python3
"""run_these_selftest.py - polaris/run_these.py against a temp store, no real files touched.

    python3 tests/run_these_selftest.py [--keep]

What it checks:
  1. add writes a file whose frontmatter and body round-trip through read_command
  2. list shows an open command, not a done or dropped one
  3. done sets status, done_at and result, and refuses a second time
  4. drop behaves the same as done, on a different command
  5. add refuses a password=/token=/secret= assignment with a literal value
  6. add refuses a URL with embedded credentials
  7. add refuses a long opaque token
  8. add does NOT refuse a real-shaped path with hyphens and a UUID (the false positive this
     was built to avoid)
  9. list --json is valid JSON with the fields the Run these card reads
  10. open_commands() with no argument reads the current run_these.STORE, not the value STORE
      held at import time (the server and the attention signal call it bare)
  11. one line or refuse: a multi-line --file, a line without "! ", a # comment, and a
      git command with no absolute cd first are all refused, with the how-to in the message;
      --note lands above the block and survives a check and a Done
  12. check, in a scratch git repo: a merged branch is stale, an unmerged one is not; a patch
      that no longer applies is stale, one that applies is not; rm of a tracked file, git reset
      --hard, push --force and the secrets files are stale (destructive); a missing script is
      stale; a done_if probe that exits 0 closes the command as done with its proof; a program
      the checker does not model is "ok (unverified)", never plain ok; a hand-written old
      multi-line command is stale; add refuses a command that is already stale
  13. check --if-due: due after 10 minutes, not before; due when main moved and touched a file
      an open command names, not when it touched something else

Exit 0 when every case passes, 1 otherwise, with the failures named.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("POLARIS_SECRETS_GLOB", "secrets.")
os.environ["POLARIS_PATH_PREFIXES"] = "tools"
from polaris import run_these  # noqa: E402

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  ok    %s" % name)
    else:
        FAILS.append("%s: %s" % (name, detail))
        print("  FAIL  %s: %s" % (name, detail))


def run(store: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "polaris.run_these", "--store", str(store), *args],
                           capture_output=True, text=True, cwd=str(REPO))


def write_cmd_file(tmp: Path, text: str) -> Path:
    p = tmp / "cmd.sh"
    p.write_text(text, encoding="utf-8")
    return p


def git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), r.stderr))
    return r.stdout.strip()


def scratch_repo(tmp: Path) -> Path:
    repo = tmp / "repo one"            # a space in the path
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    (repo / "tool.py").write_text("print('hi')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    git(repo, "checkout", "-qb", "merged-one")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "b")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge b", "merged-one")
    git(repo, "checkout", "-qb", "waiting")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c")
    git(repo, "checkout", "-q", "main")
    return repo


def add(store: Path, line: str, *extra) -> subprocess.CompletedProcess:
    return run(store, "add", "--title", "t", "--why", "w", "--from", "selftest", "--line", line, *extra)


def one_line_rule(tmp: Path) -> None:
    store = tmp / "store11"
    multi = write_cmd_file(tmp, "! cd /tmp\n# then this\nls\n")
    r = run(store, "add", "--title", "x", "--why", "y", "--from", "z", "--file", str(multi))
    check("11. a multi-line --file is refused", r.returncode == 2 and "3 lines" in r.stderr, r.stderr)
    check("11. the refusal says how to write it", "ONE line" in r.stderr and "--note" in r.stderr, r.stderr)
    r = add(store, "echo no bang")
    check("11. a line without the ! prefix is refused", r.returncode == 2 and '"! "' in r.stderr, r.stderr)
    r = add(store, "! echo hi # and a comment")
    check("11. a # comment is refused", r.returncode == 2 and "comment" in r.stderr, r.stderr)
    r = add(store, "! git status")
    check("11. git with no absolute cd first is refused", r.returncode == 2 and "cd" in r.stderr, r.stderr)
    r = add(store, "! python3 tools/x.py")
    check("11. a relative path with no cd first is refused", r.returncode == 2, r.stderr)
    check("11. nothing refused wrote a file", not list(store.glob("*.md")), list(store.glob("*.md")))
    r = add(store, "! echo '# not a comment' && echo ok", "--note", "**First** open Settings by hand.",
            "--expect", "ok")
    check("11. a quoted # is not a comment, and a good line is accepted", r.returncode == 0, r.stderr)
    files = list(store.glob("*.md"))
    rec = run_these.read_command(files[0]) if files else {}
    check("11. the stored command has no ! and no # line", rec.get("command") == "echo '# not a comment' && echo ok",
          rec.get("command"))
    check("11. --note is kept above the block", rec.get("note") == "**First** open Settings by hand.", rec.get("note"))
    check("11. add wrote a verdict", rec.get("check") == "ok" and bool(rec.get("checked_at")), rec)
    text = files[0].read_text() if files else ""
    check("11. the note sits above the fenced block", text.find("**First**") < text.find("```"), text)
    if files:
        run(store, "--state", str(tmp / "state11.json"), "check")
        run(store, "done", rec["id"])
        rec2 = run_these.read_command(files[0])
        check("11. the note survives a check and a Done", rec2.get("note") == rec["note"] and rec2["status"] == "done", rec2)


def stale_rules(tmp: Path) -> None:
    try:
        repo = scratch_repo(tmp)
    except (RuntimeError, OSError) as e:
        check("12. scratch repo", False, str(e))
        return
    store = tmp / "store12"
    state = ["--state", str(tmp / "state12.json")]
    cd = 'cd "%s" && ' % repo
    # a patch made now against a.txt, then a.txt moves on, so it no longer applies
    (repo / "a.txt").write_text("one\nTWO\nthree\n")
    stale_patch = tmp / "old.diff"
    stale_patch.write_text(git(repo, "diff") + "\n")
    git(repo, "checkout", "--", "a.txt")
    (repo / "a.txt").write_text("one\nzwei\nthree\n")
    git(repo, "commit", "-qam", "a moves on")
    (repo / "tool.py").write_text("print('hello')\n")
    good_patch = tmp / "good.diff"
    good_patch.write_text(git(repo, "diff") + "\n")
    git(repo, "checkout", "--", "tool.py")

    def verdict(line):
        return run_these.judge({"command": line, "done_if": ""})

    cases = [
        ("a merged branch is stale", cd + "git merge --no-ff merged-one", "stale: merged-one is already merged into main"),
        ("an unmerged branch is not stale", cd + "git merge --no-ff waiting", "ok"),
        ("a branch that does not exist is stale", cd + "git merge gone-branch", "stale: branch gone-branch does not exist"),
        ("a patch that no longer applies is stale", cd + 'git apply "%s"' % stale_patch, "stale: the patch"),
        ("a patch that applies is not stale", cd + 'git apply "%s"' % good_patch, "ok"),
        ("rm of a tracked file is stale (destructive)", cd + "rm tool.py", "stale: destructive, rm of tracked file"),
        ("git reset --hard is stale (destructive)", cd + "git reset --hard HEAD~1", "stale: destructive, git reset --hard"),
        ("push --force is stale (destructive)", cd + "git push --force origin main", "stale: destructive, git push --force"),
        ("anything touching the secrets files is stale", cd + "cat " + "config/" + "secrets.example.env", "stale: destructive, it touches"),
        ("a missing script is stale", cd + "python3 tools/gone.py", "stale: it names"),
        ("a cd into a missing directory is stale", 'cd "%s/nope" && ls' % repo, "stale: it changes into"),
        ("a script that exists is ok", cd + "python3 tool.py", "ok"),
        ("a path the merged-in branch brings is not missing", cd + "git merge --no-ff waiting && cat c.txt", "ok"),
        ("an unmodelled program is ok (unverified)", cd + "launchctl list", "ok (unverified): it runs launchctl"),
        ("a computed path is ok (unverified)", cd + "ls $(pwd)/x", "ok (unverified)"),
        ("an old multi-line block is stale", "cd /tmp\ngit status", "stale: more than one line"),
    ]
    for name, line, want in cases:
        got = verdict(line)
        ok = got == want if want == "ok" else got.startswith(want)
        check("12. " + name, ok, got)

    # git checkout -- over uncommitted edits
    (repo / "tool.py").write_text("print('edited')\n")
    got = verdict(cd + "git checkout -- tool.py")
    check("12. git checkout -- over uncommitted edits is stale (destructive)", got.startswith("stale: destructive"), got)
    git(repo, "checkout", "--", "tool.py")
    got = verdict(cd + "git checkout -- tool.py")
    check("12. git checkout -- of a clean file is not destructive", got == "ok", got)

    # done by its expect probe: the effect is already true
    marker = tmp / "effect-done"
    r = add(store, "! " + cd + "python3 tool.py", "--done-if", 'test -f "%s"' % marker)
    check("12. add with a --done-if whose effect is not yet true is accepted", r.returncode == 0, r.stderr)
    files = list(store.glob("*.md"))
    marker.write_text("x")
    run(store, *state, "check")
    rec = run_these.read_command(files[0]) if files else {}
    check("12. check closes a command whose done_if probe exits 0", rec.get("status") == "done", rec)
    check("12. the proof is recorded", "exited 0" in (rec.get("result") or "") and rec.get("check", "").startswith("done"), rec)
    r = add(store, "! " + cd + "python3 tool.py", "--done-if", 'test -f "%s"' % marker)
    check("12. add refuses a command whose effect is already true", r.returncode == 2 and "nothing to do" in r.stderr, r.stderr)
    r = add(store, "! " + cd + "git merge merged-one")
    check("12. add refuses a command that is already stale", r.returncode == 2 and "already merged" in r.stderr, r.stderr)

    # the verdict lands in the frontmatter of a hand-written file, body untouched
    store.mkdir(parents=True, exist_ok=True)
    hand = store / "hand0001.md"
    hand.write_text("---\nid: hand0001\ncreated: 2026-09-28T00:00:00Z\nfrom: x\ntitle: t\nwhy: w\nstatus: open\n"
                    "done_at: \nresult: \ntested: no\n---\n\nDo the Settings step by hand first.\n\n```\n! "
                    + cd + "launchctl list\n```\n\n**What you should see:** a list\n\n_rewritten by hand_\n")
    body_before = hand.read_text().split("---", 2)[2]
    run(store, *state, "check", "hand0001")
    rec = run_these.read_command(hand)
    check("12. check writes check and checked_at", rec.get("check", "").startswith("ok (unverified)") and bool(rec.get("checked_at")), rec)
    check("12. check leaves the hand-written body byte for byte", hand.read_text().split("---", 2)[2] == body_before,
          hand.read_text())
    check("12. verdict_kind reads the four verdicts", [run_these.verdict_kind(v) for v in
          ("ok", "ok (unverified): x", "stale: y", "done: z", "")] == ["ok", "unverified", "stale", "done", "unchecked"])


def due_rule(tmp: Path) -> None:
    repo = tmp / "repo one"
    if not repo.is_dir():
        return
    rows = [{"command": 'cd "%s" && python3 tool.py' % repo}]
    head = git(repo, "rev-parse", "main")
    check("13. due after 10 minutes", run_these.due(rows, {"last": 1000, "head": head}, 1000 + 601, head, repo) != "")
    check("13. not due before 10 minutes with main unchanged",
          run_these.due(rows, {"last": 1000, "head": head}, 1000 + 60, head, repo) == "")
    check("13. never due with no open commands", run_these.due([], {}, 10 ** 10, head, repo) == "")
    (repo / "other.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "other")
    h2 = git(repo, "rev-parse", "main")
    check("13. main moved but touched nothing named: not due",
          run_these.due(rows, {"last": 1000, "head": head}, 1000 + 60, h2, repo) == "")
    (repo / "tool.py").write_text("print('moved')\n")
    git(repo, "commit", "-qam", "tool moves")
    h3 = git(repo, "rev-parse", "main")
    why = run_these.due(rows, {"last": 1000, "head": h2}, 1000 + 60, h3, repo)
    check("13. main moved and touched a named file: due", "tool.py" in why, why)
    mrows = [{"command": 'cd "%s" && git merge waiting' % repo}]
    check("13. main moved and a command merges a branch: due",
          run_these.due(mrows, {"last": 1000, "head": h2}, 1000 + 60, h3, repo) != "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="polaris-run-these-selftest-"))
    store = tmp / "store"

    cmdfile = write_cmd_file(tmp, "! echo hello\n")
    r = run(store, "add", "--title", "Say hello", "--why", "auto mode blocks it",
            "--from", "sess1", "--file", str(cmdfile), "--expect", "hello printed")
    check("1. add exits 0", r.returncode == 0, r.stderr)
    files = list(store.glob("*.md"))
    check("1. add wrote one file", len(files) == 1, str(files))
    rec = run_these.read_command(files[0]) if files else {}
    check("1. round-trips command text", rec.get("command") == "echo hello", rec.get("command"))
    check("1. round-trips expect text", rec.get("expect") == "hello printed", rec.get("expect"))
    check("1. round-trips title/why/from/status", rec.get("title") == "Say hello" and rec.get("why") == "auto mode blocks it"
          and rec.get("from") == "sess1" and rec.get("status") == "open", rec)
    id1 = rec.get("id", "")

    cmdfile2 = write_cmd_file(tmp, "! echo second\n")
    run(store, "add", "--title", "Second one", "--why", "needs eyes", "--from", "sess2", "--file", str(cmdfile2))
    ids = sorted(p.stem for p in store.glob("*.md"))
    id2 = [i for i in ids if i != id1][0]

    r = run(store, "list")
    check("2. list shows both open commands", "Say hello" in r.stdout and "Second one" in r.stdout, r.stdout)

    r = run(store, "done", id1, "--result", "worked fine")
    check("3. done exits 0", r.returncode == 0, r.stderr)
    rec1 = run_these.read_command(store / (id1 + ".md"))
    check("3. status is done", rec1.get("status") == "done", rec1.get("status"))
    check("3. done_at is set", bool(rec1.get("done_at")), rec1)
    check("3. result is recorded", rec1.get("result") == "worked fine", rec1.get("result"))
    r = run(store, "done", id1)
    check("3. a second done refuses (already closed)", r.returncode != 0, r.stdout + r.stderr)

    r = run(store, "list")
    check("2. list no longer shows the done command", "Say hello" not in r.stdout and "Second one" in r.stdout, r.stdout)

    r = run(store, "drop", id2, "--result", "not needed after all")
    check("4. drop exits 0", r.returncode == 0, r.stderr)
    rec2 = run_these.read_command(store / (id2 + ".md"))
    check("4. status is dropped", rec2.get("status") == "dropped", rec2.get("status"))
    check("4. result is recorded", rec2.get("result") == "not needed after all", rec2.get("result"))

    bad_secret = write_cmd_file(tmp, '! curl -H "Authorization: password=SuperSecretValue123" https://x\n')
    r = run(store, "add", "--title", "x", "--why", "y", "--from", "z", "--file", str(bad_secret))
    check("5. refuses password= with a literal value", r.returncode != 0 and "secret" in r.stderr, r.stderr)
    check("5. refused command wrote no new file", len(list(store.glob("*.md"))) == 2, list(store.glob("*.md")))

    bad_url = write_cmd_file(tmp, "! curl https://alice:hunter2@example.com/api\n")
    r = run(store, "add", "--title", "x", "--why", "y", "--from", "z", "--file", str(bad_url))
    check("6. refuses a URL with embedded credentials", r.returncode != 0, r.stderr)

    bad_token = write_cmd_file(tmp, "! export FOO=aVeryLongOpaqueTokenValue1234567890\n")
    r = run(store, "add", "--title", "x", "--why", "y", "--from", "z", "--file", str(bad_token))
    check("7. refuses a long opaque token", r.returncode != 0, r.stderr)

    # A plausible scratch command: a path with hyphens and a UUID must not look like a token.
    # Asked of the heuristic directly, because add would also refuse the missing file it names.
    scratch = 'S=/tmp/x/00000000-0000-4000-8000-000000000042/step-two.sh; sed -n 3p "$S" && bash "$S"'
    check("8. does not refuse a hyphenated path with a UUID", run_these.looks_like_secret(scratch) == "",
          run_these.looks_like_secret(scratch))

    r = run(store, "list", "--json")
    try:
        rows = json.loads(r.stdout)
        ok = isinstance(rows, list) and all(set(run_these.FIELDS) <= set(row) for row in rows)
    except (json.JSONDecodeError, TypeError):
        ok, rows = False, r.stdout
    check("9. list --json is valid and carries every field", ok, rows)

    run(store, "add", "--title", "Third one", "--why", "stays open", "--from", "sess3", "--line", "! echo third")
    keep_store = run_these.STORE
    run_these.STORE = store
    try:
        open_ids = {r["id"] for r in run_these.open_commands()}
    finally:
        run_these.STORE = keep_store
    check("10. open_commands() with no arg honours a reassigned run_these.STORE",
          open_ids and open_ids == {r["id"] for r in run_these.all_commands(store) if r["status"] == "open"}, open_ids)

    one_line_rule(tmp)
    stale_rules(tmp)
    due_rule(tmp)

    if args.keep:
        print("kept: %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED %d" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

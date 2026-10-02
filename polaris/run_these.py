#!/usr/bin/env python3
"""run_these.py - the store of commands an agent could not run itself, waiting for you.

Some actions are blocked for unattended agents on purpose (secret writes, sending mail, settings,
installs). When a session or a run hits one of those, it hands you the exact command instead of
burying it in a report. This is the one place those commands live, so the Polaris card and any
terminal tool can show them and you can act from whichever you open first.

One markdown file per command in `$POLARIS_HOME/commands/`, filename `<id>.md`:

    ---
    id: 0a1b2c3d
    created: 2026-01-05T08:30:00Z
    from: run-1234
    title: one line, what it does in plain words
    why: why an agent could not run it
    status: open              # open | done | dropped
    done_at:
    result:
    tested: no                # yes | no -- has this exact command been run on this Mac before
    done_if: <read-only shell probe; exit 0 means the effect is already true>
    checked_at: 2026-01-05T09:00:00Z
    check: ok                 # ok | ok (unverified): <why> | stale: <reason> | done: <proof>
    ---

    <the note: everything you must know or do by hand first>

    ```
    ! <the exact command, one line>
    ```

    **What you should see:** <expect text>

Stdlib only. Runs under /usr/bin/python3 3.9.

    python3 -m polaris.run_these add --title "..." --why "..." --from RUN --line '! cd "/abs/repo" && ...' \
                   [--note "..."] [--expect "..."] [--done-if "probe"] [--tested yes|no]
    python3 -m polaris.run_these add ... --file cmd.txt   # or --file - for stdin; still exactly one line
    python3 -m polaris.run_these list [--json]            # open commands, oldest first, with their verdict
    python3 -m polaris.run_these check [ID] [--dry-run] [--if-due]
    python3 -m polaris.run_these done ID [--result "..."]
    python3 -m polaris.run_these drop ID [--result "..."]

**One line or refuse.** These are pasted into Claude Code with the `!` prefix, and into a plain
Terminal. A multi-line block with `#` comments broke both: only the first line got the `!`, and
zsh ran the prose as shell. So `add` takes exactly one line starting `! `, with no newline and no
`#` comment, and a command that touches a repo (git, or any relative path) must start
`cd "<absolute path>" &&`. Everything else goes in `--note` (shown above the block) and
`--expect`. `add` also runs the checker and refuses a command that is already stale or done.

**The checker (`check`).** One verdict per open command, written to its frontmatter:
  - `done: <proof>` when its `done_if` probe exits 0: the effect is already true. The command is
    closed with that proof as its result.
  - `stale: <reason>` when a path, script or file it names is missing, a `git merge <branch>`
    whose branch is already an ancestor of the checkout's HEAD, a `git apply <patch>` that fails
    `git apply --check` against HEAD (in a scratch index built from HEAD, so nothing is written
    to the checkout or its .git/worktrees), or when it is destructive: `rm` of a tracked file,
    `git reset --hard`, `git checkout -- <file>` over uncommitted edits, `push --force`, a branch
    switch or stash in a shared checkout (POLARIS_SHARED), or anything naming the secrets
    marker (POLARIS_SECRETS_GLOB). A multi-line or commented command is stale too.
  - `ok (unverified): <why>` when some part of it is beyond the checker (a program it does not
    model, a computed path, a checker crash). Never silently `ok`.
  - `ok` only when every part was judged.
`check --if-due` is meant for a scheduler that calls it every minute: it runs when the last
check is 10 minutes old, or when a new commit on the watched branch of POLARIS_REPO
(POLARIS_BRANCH, default main) touches a file an open command names (or any open command merges a branch). Its state is
`$POLARIS_HOME/state/run-these-check.json`.

`--tested` names whether the exact command has already been run successfully on this Mac.
`summary_line()` is a one-line digest for a morning email or a status bar.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from . import config
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from polaris import config  # type: ignore

ROOT = config.repo()
STORE = config.sub("commands")
STATE = config.state_file("run-these-check.json")
HOME = Path.home()
# The checkouts every run shares: a branch switch or a stash there moves everybody.
SHARED = config.shared()
SECRETS = config.secrets_marker()
EVERY = 600

FIELDS = ["id", "created", "from", "title", "why", "status", "done_at", "result", "tested",
          "done_if", "checked_at", "check"]

# A refusal, not a filter: these are heuristics for "do not commit this literal value to git",
# not a claim of completeness. Hyphens and slashes are excluded from the opaque-run check on
# purpose, or every kebab-case path and UUID (which breaks into dash-separated groups under 20
# chars each) and every filesystem path would trip it.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth)\s*[=:]\s*"
                r"['\"]?(?!\$|\{\{)[^\s'\"]{6,}"), "a password=/token=/secret=-style assignment with a literal value"),
    (re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"), "a URL with embedded credentials"),
    (re.compile(r"(?<![\w+])[A-Za-z0-9+_]{24,}(?![\w+])"), "a long opaque token-shaped run of characters"),
]

HOW_TO = ('Write it as ONE line starting "! ", with no # comments, for example:\n'
          '  ! cd "/path/to/repo" && python3 scripts/x.py --flag\n'
          'Put the explanation and any by-hand steps in --note "..." (shown above the block), and '
          'what you should see in --expect "...". Pass it with --line \'! ...\' or --file.')


def looks_like_secret(text: str) -> str:
    """The first reason `text` looks like it carries a secret value, or "" when it does not."""
    # macOS per-user temp dirs (/var/folders/xx/xxxxxxxxxxxx/T) are not tokens
    text = re.sub(r"/var/folders/[^/\s]+/[^/\s]+/", "/var/folders/", text)
    for pat, reason in _SECRET_PATTERNS:
        if pat.search(text):
            return reason
    return ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _fmt(value) -> str:
    if value is None:
        return ""
    s = str(value).replace("\n", " ")
    if not s:
        return ""
    if re.search(r'^[\w.:+-]+$', s):
        return s
    return '"%s"' % _esc(s)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def write_command(store: Path, rec: dict, command: str, expect: str, note: str = "") -> Path:
    store.mkdir(parents=True, exist_ok=True)
    path = store / ("%s.md" % rec["id"])
    lines = ["---"]
    for k in FIELDS:
        lines.append("%s: %s" % (k, _fmt(rec.get(k))))
    lines.append("---")
    lines.append("")
    if note:
        lines.append(note.strip())
        lines.append("")
    lines.append("```")
    lines.append("! " + command)
    lines.append("```")
    lines.append("")
    lines.append("**What you should see:** %s" % (expect or "(not recorded)"))
    lines.append("")
    _atomic_write(path, "\n".join(lines))
    return path


_FM_LINE = re.compile(r"^([A-Za-z_]+):\s*(.*)$")


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def read_command(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rec = {k: "" for k in FIELDS}
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            m = _FM_LINE.match(lines[i])
            if m and m.group(1) in FIELDS:
                rec[m.group(1)] = _unquote(m.group(2))
            i += 1
        body = "\n".join(lines[i + 1:])
    else:
        body = text
    # Older files carry a "# tested on this Mac" line above the command inside the block.
    m = re.search(r"```\s*\n(?:# tested.*\n)?! (.*?)\n```", body, re.S)
    rec["command"] = m.group(1) if m else ""
    fence = body.find("```")
    rec["note"] = body[:fence].strip() if fence > 0 else ""
    m = re.search(r"\*\*What you should see:\*\*\s*(.*)", body)
    rec["expect"] = m.group(1).strip() if m else ""
    rec["path"] = str(path)
    return rec


def set_fields(path: Path, updates: dict) -> None:
    """Rewrite only the frontmatter keys in `updates`, leaving the body byte for byte, so a
    note written by hand above the block survives a check, a Done or a Drop."""
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("%s has no frontmatter" % path)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("%s has an unclosed frontmatter" % path)
    todo = dict(updates)
    for i in range(1, end):
        m = _FM_LINE.match(lines[i])
        if m and m.group(1) in todo:
            lines[i] = "%s: %s" % (m.group(1), _fmt(todo.pop(m.group(1))))
    extra = ["%s: %s" % (k, _fmt(v)) for k, v in todo.items()]
    lines[end:end] = extra
    _atomic_write(path, "\n".join(lines))


def all_commands(store: Path) -> list:
    if not store.is_dir():
        return []
    out = []
    for p in sorted(store.glob("*.md")):
        try:
            out.append(read_command(p))
        except OSError:
            continue
    out.sort(key=lambda r: r.get("created") or "")
    return out


def open_commands(store: "Path | None" = None) -> list:
    """Open commands, oldest first: what the Run these card reads. With no argument the store is
    this module's STORE as it is when called, so a test may swap it for a scratch folder."""
    return [r for r in all_commands(STORE if store is None else store) if r.get("status") == "open"]


def verdict_kind(check: str) -> str:
    """ok | unverified | stale | done | unchecked, from a stored `check` value. The card
    decides from this which buttons to show, and a stale command gets no Copy."""
    c = (check or "").strip()
    if not c:
        return "unchecked"
    if c.startswith("stale"):
        return "stale"
    if c.startswith("done"):
        return "done"
    if c.startswith("ok (unverified)"):
        return "unverified"
    if c == "ok":
        return "ok"
    return "unverified"


def find(store: Path, id_: str) -> Path:
    path = store / ("%s.md" % id_)
    if not path.is_file():
        raise SystemExit("no command %r in %s" % (id_, store))
    return path


# ---------------------------------------------------------------------------------------------
# A small shell lexer: enough of POSIX quoting to split a one-line command into segments and
# words, and to say which words are computed at run time ($VAR, $(...), globs).

def _match_paren(s: str, k: int) -> int:
    depth, i, n = 0, k, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = s.find(c, i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def lex(s: str):
    """(tokens, has_comment). A token is ("w", value, dynamic), ("op", text) for a control
    operator or a newline, or ("redir", text). `~` at the start of a word is expanded."""
    toks, i, n, comment = [], 0, len(s), False
    st = {"buf": None, "dyn": False, "tilde": False}

    def flush():
        if st["buf"] is not None:
            v = st["buf"]
            if st["tilde"] and (v == "~" or v.startswith("~/")):
                v = str(HOME) + v[1:]
            toks.append(("w", v, st["dyn"]))
        st.update(buf=None, dyn=False, tilde=False)

    while i < n:
        c = s[i]
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "\n":
            flush()
            toks.append(("op", "\n"))
            i += 1
            continue
        if c == "#" and st["buf"] is None:
            comment = True
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "&|;()":
            flush()
            two = s[i:i + 2]
            if two in ("&&", "||", ";;", "|&"):
                toks.append(("op", two))
                i += 2
            elif two == "&>":
                toks.append(("redir", ">"))
                i += 2
            else:
                toks.append(("op", c))
                i += 1
            continue
        if c in "<>":
            if st["buf"] is not None and st["buf"].isdigit() and not st["dyn"]:
                st.update(buf=None)          # the fd number in 2> belongs to the redirection
            flush()
            j = i + 1
            if j < n and s[j] == c:
                j += 1
            if j < n and s[j] == "&":        # 2>&1: fd duplication, no target word
                j += 1
                while j < n and (s[j].isdigit() or s[j] == "-"):
                    j += 1
                i = j
                continue
            if j < n and s[j] == "|":
                j += 1
            toks.append(("redir", s[i:j]))
            i = j
            continue
        if st["buf"] is None:
            st["buf"] = ""
            st["tilde"] = c == "~"
        if c == "\\":
            st["buf"] += s[i + 1] if i + 1 < n else ""
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                st["dyn"] = True
                st["buf"] += s[i + 1:]
                i = n
                continue
            st["buf"] += s[i + 1:j]
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    st["buf"] += s[j + 1] if s[j + 1] in '"\\$`' else s[j:j + 2]
                    j += 2
                    continue
                if s[j] == "$" and j + 1 < n and s[j + 1] == "(":
                    k = _match_paren(s, j + 1)
                    st["buf"] += s[j:k + 1]
                    st["dyn"] = True
                    j = k + 1
                    continue
                if s[j] in "$`":
                    st["dyn"] = True
                st["buf"] += s[j]
                j += 1
            if j >= n:
                st["dyn"] = True
            i = j + 1
            continue
        if c == "$":
            st["dyn"] = True
            if i + 1 < n and s[i + 1] == "(":
                k = _match_paren(s, i + 1)
                st["buf"] += s[i:k + 1]
                i = k + 1
                continue
        elif c == "`":
            st["dyn"] = True
            j = s.find("`", i + 1)
            j = n - 1 if j < 0 else j
            st["buf"] += s[i:j + 1]
            i = j + 1
            continue
        elif c in "*?[" and not (st["buf"] == "" and s[i:i + 2] in ("[ ", "[[")):
            st["dyn"] = True
        st["buf"] += c
        i += 1
    flush()
    return toks, comment


def segments(toks) -> list:
    """Simple commands: {"words": [(v, dyn)], "out": [...], "in": [...]}."""
    segs, cur, pending = [], {"words": [], "out": [], "in": []}, None
    for t in toks:
        if t[0] == "op":
            if cur["words"] or cur["out"] or cur["in"]:
                segs.append(cur)
            cur, pending = {"words": [], "out": [], "in": []}, None
            continue
        if t[0] == "redir":
            pending = t[1]
            continue
        w = (t[1], t[2])
        if pending:
            if pending.startswith("<<"):
                pass
            elif pending.startswith("<"):
                cur["in"].append(w)
            else:
                cur["out"].append(w)
            pending = None
            continue
        cur["words"].append(w)
    if cur["words"] or cur["out"] or cur["in"]:
        segs.append(cur)
    return segs


# ---------------------------------------------------------------------------------------------
# The analysis: what a command names, what it would do, and what cannot be judged.

_PATH_PREFIX = ("docs/", "scripts/", "src/", "tests/", ".claude/", "./", "../") + tuple(
    p.strip().rstrip("/") + "/" for p in (os.environ.get("POLARIS_PATH_PREFIXES") or "").split(",") if p.strip())
_PATH_EXT = re.compile(r"\.(py|sh|md|json|jsonl|diff|patch|plist|txt|tsv|html|js|ts|swift|ya?ml|toml|csv)$")
_READERS = {"cat", "ls", "head", "tail", "wc", "less", "more", "stat", "file", "diff", "cmp", "du",
            "open", "shasum", "md5", "plutil", "xattr", "jq", "sort", "uniq"}
_TEXT = {"echo", "printf", "sleep", "true", "false", "pwd", "date", "which", "type", "id",
         "whoami", "cut", "tr", "test", "[", "[[", "basename", "dirname", "say"}
_INTERP = re.compile(r"^(python[0-9.]*|bash|sh|zsh|node|osascript|ruby|perl)$")
_CREATORS = {"mkdir", "touch"}
_COPIERS = {"cp", "mv", "ln", "install", "rsync", "ditto"}
_WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "!", "caffeinate"}


def _git(repo, *args, env=None, timeout=30):
    return subprocess.run(["git", "-C", str(repo)] + [str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout, env=env)


def _short(p) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def _pathish(v: str, loose: bool) -> bool:
    if not v or v.startswith("-") or "://" in v:
        return False
    if v.startswith("/"):
        return True
    if loose:
        return "/" in v or bool(_PATH_EXT.search(v))
    return v.startswith(_PATH_PREFIX) or bool(_PATH_EXT.search(v))


def _positional(words, takes_value=()):
    out, skip, dashdash = [], False, False
    for v, dyn in words:
        if skip:
            skip = False
            continue
        if not dashdash and v == "--":
            dashdash = True
            continue
        if not dashdash and v.startswith("-") and len(v) > 1:
            if v in takes_value:
                skip = True
            continue
        out.append((v, dyn))
    return out


def apply_check(repo: Path, patch: Path, flags=()) -> str:
    """"" when `git apply --check` passes against HEAD, else the first error line. Runs in a
    scratch index read from HEAD: the same test as a scratch worktree, but nothing is written to
    the checkout or to its .git/worktrees, which every other run shares."""
    fd, idx = tempfile.mkstemp(prefix="polaris-run-these-index-")
    os.close(fd)
    os.unlink(idx)
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    keep = [f for f in flags if f not in ("--check", "--index", "--cached", "--3way", "-3", "--reject",
                                          "-v", "--verbose", "--stat", "--numstat", "--summary", "--apply")]
    try:
        r = _git(repo, "read-tree", "HEAD", env=env)
        if r.returncode:
            return "could not read HEAD: " + (r.stderr.strip().splitlines() or ["?"])[0]
        r = _git(repo, "apply", "--cached", "--check", *keep, str(patch), env=env)
        if r.returncode == 0:
            return ""
        return (r.stderr.strip().splitlines() or ["git apply --check failed"])[0]
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


class Analysis:
    def __init__(self):
        self.stale, self.destructive, self.unverified = [], [], []
        self.named, self.merges = set(), []
        self.comment = False
        self.multiline = False
        self.touches_repo = False
        self.relative = False
        self.first_cd = None


def analyse(command: str, deep: bool = True) -> Analysis:
    """Walk the command segment by segment, tracking `cd`. With deep=False no git or file
    probes run beyond path existence: enough for `named` (the scheduler's due test)."""
    A = Analysis()
    A.multiline = "\n" in command
    toks, A.comment = lex(command)
    if SECRETS and SECRETS in command:
        A.destructive.append("it touches a secrets file (%s)" % SECRETS)
    segs = segments(toks)
    cwd = None
    created, applied, tops = set(), set(), {}

    def top(p):
        p = Path(p)
        probe = p if p.is_dir() else p.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        key = str(probe)
        if key not in tops:
            try:
                r = _git(probe, "rev-parse", "--show-toplevel", timeout=20)
                tops[key] = Path(r.stdout.strip()) if r.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                tops[key] = None
        return tops[key]

    def resolve(v):
        # realpath, so /var/folders and /tmp compare equal to what git --show-toplevel prints
        if v.startswith("/"):
            return Path(os.path.realpath(v))
        if cwd is None:
            return None
        return Path(os.path.realpath(str(cwd / v)))

    def made(p):
        return any(p == c or c in p.parents for c in created)

    def check_path(w, loose=True, must=True):
        v, dyn = w
        if dyn:
            if "/" in v or _pathish(v, loose):
                A.unverified.append("a path computed at run time (%s)" % v[:40])
            return None
        if not _pathish(v, loose):
            return None
        p = resolve(v)
        if p is None:
            A.relative = True
            A.unverified.append("a relative path with no cd first (%s)" % v)
            return None
        A.named.add(p)
        if not must or p.exists() or made(p):
            return p
        for repo, branch in A.merges:
            if repo in p.parents and deep:
                try:
                    if _git(repo, "cat-file", "-e", "%s:%s" % (branch, p.relative_to(repo))).returncode == 0:
                        return p
                except (OSError, subprocess.SubprocessError):
                    pass
        if any(r in p.parents for r in applied):
            A.unverified.append("%s may come from the patch it applies" % _short(p))
            return p
        A.stale.append("it names %s, which does not exist" % _short(p))
        return None

    shell_vars = {"HOME": str(HOME)}

    def subst(w):
        # "$S" or "${S}/x" after S=/abs/path: a plain variable the command set itself.
        m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(/.*)?$", w[0])
        if w[1] and m and m.group(1) in shell_vars and "$" not in (m.group(2) or ""):
            return (shell_vars[m.group(1)] + (m.group(2) or ""), False)
        return w

    for n_seg, seg in enumerate(segs):
        seg = {k: [subst(w) for w in v] for k, v in seg.items()}
        words = list(seg["words"])
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0][0]):
            k, _, v = words[0][0].partition("=")
            if words[0][1]:
                shell_vars.pop(k, None)
            else:
                shell_vars[k] = v
            words = words[1:]
        while words and words[0][0] in _WRAPPERS | {"gtimeout", "timeout"}:
            if words[0][0] == "sudo":
                A.unverified.append("it runs with sudo")
            words = words[2:] if words[0][0] in ("gtimeout", "timeout") else words[1:]
        for w in seg["in"]:
            check_path(w)
        for v, dyn in seg["out"]:
            if not dyn and v != "/dev/null":
                p = resolve(v)
                if p is not None:
                    created.add(p)
        if not words:
            continue
        prog, pdyn = words[0]
        args = words[1:]
        if pdyn:
            A.unverified.append("it runs a command computed at run time")
            continue
        name = os.path.basename(prog)
        if "/" in prog:
            check_path((prog, False))

        if name == "cd":
            if not args:
                cwd = HOME
                continue
            v, dyn = args[0]
            if dyn:
                cwd = None
                A.unverified.append("it changes into a directory computed at run time")
                continue
            p = resolve(v)
            if p is None:
                A.relative = True
                A.unverified.append("a relative cd with no absolute cd first (%s)" % v)
                cwd = None
                continue
            if n_seg == 0 and v.startswith("/"):
                A.first_cd = p
            if not p.is_dir() and not made(p):
                A.stale.append("it changes into %s, which does not exist" % _short(p))
                cwd = None
                continue
            cwd = p
        elif name == "git":
            cwd_before = cwd
            _analyse_git(A, args, cwd, check_path, resolve, top, created, applied, deep)
            cwd = cwd_before
        elif name in ("rm", "rmdir", "unlink"):
            for v, dyn in _positional(args):
                if dyn:
                    A.unverified.append("it deletes a path computed at run time")
                    continue
                p = resolve(v)
                if p is None:
                    A.relative = True
                    A.unverified.append("rm of a relative path with no cd first")
                    continue
                A.named.add(p)
                if not p.exists() and not p.is_symlink():
                    if not made(p):
                        A.stale.append("it deletes %s, which is already gone" % _short(p))
                    continue
                tracked = False
                repo = top(p) if deep else None
                if repo is not None:
                    try:
                        r = _git(repo, "ls-files", "--", str(p.relative_to(repo)) if p != repo else ".")
                        tracked = bool(r.stdout.strip())
                    except (OSError, subprocess.SubprocessError, ValueError):
                        A.unverified.append("could not ask git whether %s is tracked" % _short(p))
                        continue
                if tracked:
                    A.destructive.append("rm of tracked file %s" % _short(p))
                elif not str(p).startswith(("/tmp/", "/private/tmp/", "/private/var/folders/")):
                    A.unverified.append("it deletes %s, which git does not track, so nothing brings it back"
                                        % _short(p))
        elif name in _CREATORS:
            for v, dyn in _positional(args):
                p = None if dyn else resolve(v)
                if p is not None:
                    created.add(p)
        elif name in _COPIERS:
            pos = _positional(args, ("-t",))
            for w in pos[:-1]:
                check_path(w)
            if pos and not pos[-1][1]:
                p = resolve(pos[-1][0])
                if p is not None:
                    created.add(p)
            A.unverified.append("%s can overwrite what is at its destination" % name)
        elif name in ("grep", "egrep", "fgrep", "rg"):
            vals = [v for v, _ in args]
            pos = _positional(args, ("-e", "-f", "-m", "-A", "-B", "-C", "--include", "--exclude"))
            if not any(v in ("-e", "-f") for v in vals):
                pos = pos[1:]
            for w in pos:
                check_path(w)
        elif name == "sed":
            vals = [v for v, _ in args]
            if any(v == "-i" or v.startswith("-i") for v in vals):
                A.unverified.append("sed -i edits a file in place")
            pos = _positional(args, ("-e", "-f"))
            if not any(v in ("-e", "-f") for v in vals):
                pos = pos[1:]
            for w in pos:
                check_path(w)
        elif name == "find":
            vals = [v for v, _ in args]
            for w in args:
                if w[0].startswith(("-", "(", "!")):
                    break
                check_path(w)
            if any(v in ("-delete", "-exec", "-execdir", "-ok") for v in vals):
                A.unverified.append("find runs -delete or -exec")
        elif _INTERP.match(name):
            vals = [v for v, _ in args]
            if any(v in ("-c", "-e") for v in vals):
                A.unverified.append("%s runs inline code" % name)
            elif "-m" in vals:
                A.unverified.append("%s -m %s" % (name, vals[vals.index("-m") + 1] if vals.index("-m") + 1 < len(vals) else ""))
            else:
                pos = _positional(args)
                if not pos:
                    A.unverified.append("%s reads a script from stdin" % name)
                else:
                    v, dyn = pos[0]
                    if dyn:
                        A.unverified.append("%s runs a script computed at run time" % name)
                    elif not v.startswith("/") and cwd is None:
                        A.relative = True
                    check_path(pos[0], loose=True)
                    for w in pos[1:]:
                        check_path(w, loose=False)
        elif name in _READERS:
            for w in _positional(args):
                check_path(w)
        elif name in _TEXT:
            pass
        else:
            A.unverified.append("it runs %s, which the checker does not model" % name)
            for w in _positional(args):
                check_path(w, loose=False)
    return A


_GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "fetch", "merge-base", "tag",
             "cat-file", "describe", "shortlog", "blame", "grep", "commit", "branch", "rev-list",
             "worktree", "add", "merge", "apply", "reset", "checkout", "restore", "switch", "stash",
             "push", "clean"}


def _analyse_git(A, args, cwd, check_path, resolve, top, created, applied, deep):
    g = list(args)
    gcwd = cwd
    while g and g[0][0].startswith("-"):
        if g[0][0] == "-C" and len(g) > 1:
            gcwd = None if g[1][1] else resolve(g[1][0])
            g = g[2:]
        elif g[0][0] == "-c":
            g = g[2:]
        else:
            g = g[1:]
    A.touches_repo = True
    if not g:
        return
    sub, rest = g[0][0], g[1:]
    vals = [v for v, _ in rest]
    if sub not in _GIT_READ:
        A.unverified.append("git %s, which the checker does not model" % sub)
        return
    if gcwd is None:
        A.relative = True
        A.unverified.append("git %s with no cd first" % sub)
        return
    if sub == "push":
        if any(v in ("-f", "--force") or v.startswith("--force") or v.startswith("+") for v in vals):
            A.destructive.append("git push --force")
        else:
            A.unverified.append("git push publishes, the checker cannot judge the remote")
        return
    if sub == "reset":
        if "--hard" in vals:
            A.destructive.append("git reset --hard")
        return
    if sub == "clean":
        if any(v == "--force" or (v.startswith("-") and not v.startswith("--") and "f" in v) for v in vals):
            A.destructive.append("git clean deletes untracked files")
        return
    if sub == "branch":
        if "-D" in vals or ("-d" in vals and ("--force" in vals or "-f" in vals)):
            A.destructive.append("git branch -D deletes a branch that may not be merged")
        return
    if not deep:
        if sub == "merge":
            A.merges.append((None, ""))
        for w in _positional(rest):
            if sub in ("add", "apply"):
                check_path(w, must=False)
        return
    repo = top(gcwd) if (gcwd.exists()) else None
    if repo is None:
        if any(gcwd == c or c in gcwd.parents for c in created):
            A.unverified.append("git %s in a checkout this command creates" % sub)
        else:
            A.stale.append("%s is not a git checkout" % _short(gcwd))
        return
    shared = repo in SHARED

    def is_branch(v):
        return _git(repo, "rev-parse", "--verify", "--quiet", v + "^{commit}").returncode == 0

    if sub == "merge":
        if any(v in ("--abort", "--continue", "--quit") for v in vals):
            A.unverified.append("git merge %s" % vals[0])
            return
        for v, dyn in _positional(rest, ("-m", "-s", "-X", "-F", "--strategy", "--strategy-option", "--file")):
            if dyn:
                A.unverified.append("git merge of a branch computed at run time")
                continue
            if not is_branch(v):
                A.stale.append("branch %s does not exist in %s" % (v, _short(repo)))
                continue
            if _git(repo, "merge-base", "--is-ancestor", v, "HEAD").returncode == 0:
                head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
                sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
                A.stale.append("%s is already merged into %s (%s)" % (v, head, sha))
                continue
            A.merges.append((repo, v))
    elif sub == "apply":
        flags = [v for v in vals if v.startswith("-")]
        if "--check" in vals:
            return
        for w in _positional(rest, ("-p", "-C", "--directory", "--exclude", "--include")):
            p = check_path(w, loose=True)
            if p is None or not p.is_file():
                continue
            err = apply_check(repo, p, flags)
            if err:
                A.stale.append("the patch %s no longer applies to %s: %s" % (_short(p), _short(repo), err))
            else:
                applied.add(repo)
    elif sub in ("checkout", "restore", "switch"):
        if sub == "switch" or (sub == "checkout" and "--" not in vals):
            pos = _positional(rest, ("-b", "-B", "--orphan"))
            branchy = sub == "switch" or any(v in ("-b", "-B", "--orphan") for v in vals) or \
                (pos and not pos[0][1] and is_branch(pos[0][0]))
            if branchy:
                if shared:
                    A.destructive.append("it switches the branch of the shared checkout %s" % _short(repo))
                return
        files = [w for w in (_positional(rest) if sub == "restore" else rest[vals.index("--") + 1:]
                             if "--" in vals else _positional(rest))]
        for v, dyn in files:
            if dyn:
                A.unverified.append("git %s of a path computed at run time" % sub)
                continue
            p = resolve(v)
            if p is None:
                continue
            try:
                r = _git(repo, "status", "--porcelain", "--", str(p.relative_to(repo)) if p != repo else ".")
            except ValueError:
                continue
            if r.stdout.strip():
                A.destructive.append("git %s would throw away uncommitted edits in %s" % (sub, _short(p)))
    elif sub == "stash":
        if not (vals and vals[0] in ("list", "show")) and shared:
            A.destructive.append("it stashes the shared checkout %s" % _short(repo))
    elif sub == "add":
        for v, dyn in _positional(rest):
            if v in (".", ":/") or dyn:
                continue
            p = resolve(v)
            if p is None or p.exists():
                if p is not None:
                    A.named.add(p)
                continue
            try:
                tracked = _git(repo, "ls-files", "--", str(p.relative_to(repo))).stdout.strip()
            except ValueError:
                tracked = ""
            if not tracked:
                check_path((v, dyn))
    elif sub == "worktree":
        if vals and vals[0] == "add":
            pos = _positional(rest[1:], ("-b", "-B", "--reason"))
            if pos and not pos[0][1]:
                p = resolve(pos[0][0])
                if p is not None:
                    if p.exists():
                        A.stale.append("worktree path %s already exists" % _short(p))
                    created.add(p)
        elif vals and vals[0] == "remove" and "--force" in vals:
            A.destructive.append("git worktree remove --force")


def _dedupe(items) -> list:
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def run_probe(probe: str, timeout: int = 120):
    r = subprocess.run(["/bin/bash", "-c", probe], cwd=str(ROOT), capture_output=True, text=True,
                       timeout=timeout)
    out = (r.stdout.strip().splitlines() or [""])[-1][:80]
    return r.returncode, out


def judge(rec: dict, probe: bool = True) -> str:
    """The verdict string for one command: done: / stale: / ok (unverified): / ok."""
    cmd = rec.get("command") or ""
    if not cmd.strip():
        return "stale: the command is empty or could not be read from its file"
    done_if = (rec.get("done_if") or "").strip()
    probe_note = ""
    if done_if and probe:
        pa = analyse(done_if)
        if pa.destructive or "\n" in done_if:
            probe_note = "its done_if probe was not run (%s)" % ("; ".join(pa.destructive) or "more than one line")
        else:
            try:
                rc, out = run_probe(done_if)
                if rc == 0:
                    return "done: the effect is already true, `%s` exited 0%s" % (
                        done_if, (" (%s)" % out) if out else "")
            except subprocess.TimeoutExpired:
                probe_note = "its done_if probe timed out"
            except OSError as e:
                probe_note = "its done_if probe could not run (%s)" % e
    if "\n" in cmd:
        return ("stale: more than one line, so only the first line gets the ! prefix and a plain "
                "shell runs the rest; rewrite it as one line with python3 -m polaris.run_these add")
    a = analyse(cmd)
    if a.comment:
        return "stale: it carries a # comment, which a plain shell runs as nothing or as prose; rewrite it with the words in --note"
    if a.destructive:
        return "stale: destructive, " + "; ".join(_dedupe(a.destructive))
    if a.stale:
        return "stale: " + "; ".join(_dedupe(a.stale))
    why = _dedupe(a.unverified + ([probe_note] if probe_note else []))
    if why:
        shown = why[:3] + (["%d more" % (len(why) - 3)] if len(why) > 3 else [])
        return "ok (unverified): " + "; ".join(shown)
    return "ok"


# ---------------------------------------------------------------------------------------------

def _read_line(args) -> str:
    if args.line is not None:
        text = args.line
    elif args.file == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.file).read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text.rstrip("\r")


def cmd_add(args) -> int:
    text = _read_line(args)
    if not text.strip():
        print("refuses: the command is empty", file=sys.stderr)
        return 2
    if "\n" in text:
        print("refuses: the command is %d lines. Only the first line of a pasted block gets the ! "
              "prefix, and a plain shell would run every later line as a command."
              % (text.count("\n") + 1), file=sys.stderr)
        print(HOW_TO, file=sys.stderr)
        return 2
    if not text.startswith("! "):
        print('refuses: the line must start with "! " (Claude Code\'s shell prefix).', file=sys.stderr)
        print(HOW_TO, file=sys.stderr)
        return 2
    command = text[2:].strip()
    reason = looks_like_secret(command) or looks_like_secret(args.done_if or "")
    if reason:
        print("refuses: this command looks like it carries a secret value (%s)." % reason, file=sys.stderr)
        print("Keep the secret in a file or read it from stdin at run time instead of "
              "writing it into the stored command.", file=sys.stderr)
        return 2
    a = analyse(command)
    if a.comment:
        print("refuses: the line carries a # comment. Put the words in --note.", file=sys.stderr)
        print(HOW_TO, file=sys.stderr)
        return 2
    if (a.touches_repo or a.relative) and a.first_cd is None:
        print('refuses: it touches a repo or a relative path, so it must start with cd "<absolute path>" && '
              "(your terminal can be anywhere).", file=sys.stderr)
        print(HOW_TO, file=sys.stderr)
        return 2
    if args.done_if and "\n" in args.done_if:
        print("refuses: --done-if must be one line", file=sys.stderr)
        return 2
    rec = {"id": secrets.token_hex(4), "created": _now(), "from": args.frm, "title": args.title,
           "why": args.why, "status": "open", "done_at": "", "result": "", "tested": args.tested,
           "done_if": args.done_if or "", "command": command}
    verdict = judge(rec)
    kind = verdict_kind(verdict)
    if kind == "stale":
        print("refuses: the checker says %s" % verdict, file=sys.stderr)
        return 2
    if kind == "done":
        print("refuses: nothing to do, the checker says %s" % verdict, file=sys.stderr)
        return 2
    rec["checked_at"] = _now()
    rec["check"] = verdict
    path = write_command(args.store, rec, command, args.expect or "", args.note or "")
    print("added %s -> %s" % (rec["id"], path))
    print("check: %s" % verdict)
    return 0


def cmd_list(args) -> int:
    rows = [r for r in all_commands(args.store) if r.get("status") == "open"]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("nothing waiting for you")
        return 0
    for r in rows:
        print("%s  %-6s  tested=%-3s  %s  (from %s)\n          check: %s%s"
              % (r["id"], r.get("status", ""), r.get("tested") or "no", r.get("title", ""),
                 r.get("from", ""), r.get("check") or "not checked yet",
                 (" at " + r["checked_at"]) if r.get("checked_at") else ""))
    return 0


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, st: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(st, indent=1) + "\n")
    except OSError:
        pass


def _branch_head(repo: Path) -> str:
    """The commit at the tip of the watched branch (POLARIS_BRANCH, default main), or ""."""
    try:
        r = _git(repo, "rev-parse", "--verify", "--quiet", config.branch() + "^{commit}", timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def due(rows: list, state: dict, now: float, head: str, repo: Path = None, every: int = EVERY) -> str:
    """Why a check is due now, or "" when it is not: the last check is `every` seconds old, or
    the watched branch (POLARIS_BRANCH) moved and the new commits touch a file an open command
    names (or any open command merges a branch, which any new commit can make stale)."""
    repo = Path(os.path.realpath(str(repo or ROOT)))
    if not rows:
        return ""
    if now - float(state.get("last") or 0) >= every:
        return "%d min since the last check" % (every // 60)
    old = state.get("head") or ""
    if not head or not old or head == old:
        return ""
    named, merges = set(), False
    for r in rows:
        a = analyse(r.get("command") or "", deep=False)
        merges = merges or bool(a.merges)
        for p in a.named:
            try:
                named.add(str(p.relative_to(repo)))
            except ValueError:
                continue
    br = config.branch()
    if merges:
        return "%s moved and a command merges a branch" % br
    try:
        changed = _git(repo, "diff", "--name-only", old, head).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return "%s moved" % br
    for f in changed:
        f = f.strip()
        if f and any(f == n or f.startswith(n.rstrip("/") + "/") for n in named if n and n != "."):
            return "%s moved and touched %s" % (br, f)
    return ""


def cmd_check(args) -> int:
    if args.if_due:
        # The scheduler starts one every minute in the background; a slow check (a git apply
        # --check on a big repo) must not be joined by a second one writing the same files.
        import fcntl
        try:
            args.state.parent.mkdir(parents=True, exist_ok=True)
            lock = open(str(args.state) + ".lock", "w")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
    rows = all_commands(args.store)
    rows = [r for r in rows if (r["id"] == args.id if args.id else r.get("status") == "open")]
    state = _load_state(args.state)
    now = time.time()
    head = _branch_head(ROOT)
    if args.if_due:
        why = due(rows, state, now, head)
        if not why:
            if head and head != state.get("head"):
                state["head"] = head
                if not args.dry_run:
                    _save_state(args.state, state)
            return 0
        print("check due: %s" % why)
    if args.id and not rows:
        print("no command %r" % args.id, file=sys.stderr)
        return 1
    for r in rows:
        try:
            verdict = judge(r, probe=not args.no_probe)
        except Exception as e:  # noqa: BLE001  a crashed checker is "unverified", never "ok"
            verdict = "ok (unverified): the checker failed (%s: %s)" % (type(e).__name__, e)
        print("%s  %s" % (r["id"], verdict))
        if args.dry_run:
            continue
        path = Path(r["path"])
        try:
            if read_command(path).get("status") != "open":
                continue
            upd = {"checked_at": _now(), "check": verdict}
            if verdict_kind(verdict) == "done":
                upd.update(status="done", done_at=_now(),
                           result="Closed by python3 -m polaris.run_these check: " + verdict[len("done: "):])
            set_fields(path, upd)
        except (OSError, ValueError) as e:
            print("  could not write the verdict: %s" % e, file=sys.stderr)
    if not args.dry_run and not args.id:
        _save_state(args.state, {"last": now, "head": head})
    return 0


def summary_line(store: "Path | None" = None, limit: int = 3) -> str:
    """A one-line digest (for a morning email or a status bar): the open count and the top `limit` titles, oldest first.
    One line if the queue is empty, else that line plus one line per command shown."""
    rows = open_commands(store)
    if not rows:
        return "Run these: nothing waiting for you."
    head = "Run these: %d waiting for you." % len(rows)
    shown = rows[:limit]
    lines = [head] + ["  %s  %s%s" % (r["id"], r.get("title", ""),
                                      "  [STALE, do not run]" if verdict_kind(r.get("check")) == "stale" else "")
                      for r in shown]
    if len(rows) > limit:
        lines.append("  ...and %d more (`python3 -m polaris.run_these list`)." % (len(rows) - limit))
    return "\n".join(lines)


def _close(store: Path, id_: str, status: str, result: str) -> int:
    path = find(store, id_)
    rec = read_command(path)
    if rec.get("status") != "open":
        print("%s is already %s, not open" % (id_, rec.get("status")), file=sys.stderr)
        return 1
    set_fields(path, {"status": status, "done_at": _now(), "result": result or ""})
    print("%s -> %s" % (id_, status))
    return 0


def cmd_done(args) -> int:
    return _close(args.store, args.id, "done", args.result)


def cmd_drop(args) -> int:
    return _close(args.store, args.id, "dropped", args.result)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run_these", description=__doc__.split("\n\n")[0])
    p.add_argument("--store", type=Path, default=STORE, help=argparse.SUPPRESS)
    p.add_argument("--state", type=Path, default=STATE, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action", required=True)

    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--why", required=True)
    a.add_argument("--from", dest="frm", required=True)
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--line", help="the command itself: one line starting '! '")
    src.add_argument("--file", help="path to a file holding that one line, or - for stdin")
    a.add_argument("--tested", default="no", choices=["yes", "no"],
                   help="has this exact command already been run successfully on this Mac")
    a.add_argument("--note", default="", help="everything you must know or do by hand first, shown above the block")
    a.add_argument("--expect", default="")
    a.add_argument("--done-if", default="",
                   help="a read-only one-line probe; exit 0 means the effect is already true, and "
                        "the checker closes the command with that proof")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list")
    l.add_argument("--json", action="store_true")
    l.set_defaults(fn=cmd_list)

    c = sub.add_parser("check")
    c.add_argument("id", nargs="?")
    c.add_argument("--dry-run", action="store_true", help="print verdicts, write nothing")
    c.add_argument("--if-due", action="store_true", help="only when 10 min have passed, or a new commit on the watched branch touched a named file")
    c.add_argument("--no-probe", action="store_true", help="do not run done_if probes")
    c.set_defaults(fn=cmd_check)

    d = sub.add_parser("done")
    d.add_argument("id")
    d.add_argument("--result", default="")
    d.set_defaults(fn=cmd_done)

    r = sub.add_parser("drop")
    r.add_argument("id")
    r.add_argument("--result", default="")
    r.set_defaults(fn=cmd_drop)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

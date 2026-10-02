"""Real terminals in tabs: a child on a pty per tab, under tmux when it is installed.

Tabs are "shell-1" .. "shell-9" (your login shell in POLARIS_REPO) and "session" (the command
in POLARIS_SESSION_CMD, default `claude`). Under tmux each tab lives in a session named
polaris-shell, polaris-shell-2, ... or polaris-session, so closing Polaris only detaches and
`tmux attach -t polaris-shell` from Terminal.app reaches the same shell. Without tmux the children
belong to the server and end with it.

One condition, PTY_COND, is shared by every pty so one long-poll (pty_reads) wakes for output in
any tab: a browser holds six connections to a host, and a poll per tab would starve the page.
Stdlib only (pty.fork, termios, fcntl).
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config

PTY_ARGV: list | None = None             # the self-test sets this to run /bin/sh or /bin/cat in the pty, never Claude Code
PTY_KEEP = 1 << 20                       # bytes of pty output kept for a page that reconnects
PTY_MAX = 6                              # terminal tabs at once, shells and the session together
PTY_TAB = re.compile(r"session|shell-[1-9]\d?")
# One condition for every pty, so one long-poll (/api/pty/reads) wakes for output in any tab: a
# browser holds six connections to a host, and a poll per tab would starve the rest of Polaris.
PTY_COND = threading.Condition()
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78MDEHc])")


def login_shell() -> str:
    """Your login shell, from the user database rather than $SHELL, which launchd may not set."""
    try:
        import pwd
        sh = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, ImportError):
        sh = ""
    for c in (sh, os.environ.get("SHELL", ""), "/bin/zsh", "/bin/sh"):
        if c and os.access(c, os.X_OK):
            return c
    return "/bin/sh"


def session_cmd() -> list:
    """What the session tab runs: POLARIS_SESSION_CMD split like a shell would, default `claude`."""
    return shlex.split(os.environ.get("POLARIS_SESSION_CMD") or "claude")


def tmux_name(tab: str) -> str:
    """The tmux session a tab lives in: session -> polaris-session, shell-1 -> polaris-shell,
    shell-2 -> polaris-shell-2. `tmux attach -t polaris-shell` from Terminal.app reaches the same one.
    POLARIS_TMUX_PREFIX changes "polaris" (the self-test uses a scratch prefix)."""
    pre = os.environ.get("POLARIS_TMUX_PREFIX") or "polaris"
    if tab == "session":
        return f"{pre}-session"
    n = tab.split("-", 1)[1] if "-" in tab else "1"
    return f"{pre}-shell" if n == "1" else f"{pre}-shell-{n}"


def session_argv(path: str | None = None, kind: str = "session", name: str | None = None) -> tuple[list, bool]:
    """What a terminal tab runs in its pty, and whether it survives Polaris closing.
    kind "session": `tmux new -A -s polaris-session -c <repo> '<POLARIS_SESSION_CMD>'`.
    kind "shell": `tmux new -A -s polaris-shell -c <repo> '<login shell> -l'`, your own shell.
    Under tmux, closing the desk only detaches and the next open reattaches. Without tmux the
    child belongs to the desk process and ends with it."""
    tmux = shutil.which("tmux", path=path if path is not None else os.environ.get("PATH"))
    if kind == "shell":
        sh = login_shell()
        if tmux:
            return [tmux, "new", "-A", "-s", name or "polaris-shell", "-c", str(config.repo()), shlex.quote(sh) + " -l"], True
        return [sh, "-l"], False
    cmd = session_cmd()
    if tmux:
        # tmux hands a lone command to the shell as a string, so a path with a space in it would
        # split: every word goes in quoted.
        return [tmux, "new", "-A", "-s", name or "polaris-session", "-c", str(config.repo()),
                " ".join(shlex.quote(w) for w in cmd)], True
    return cmd, False


def pty_env(cols: int, rows: int) -> dict:
    """The child's environment. No COLUMNS or LINES: a tmux server started from here would hand
    them to every shell it ever starts, and programs trust them over the real window size."""
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES", "TMUX", "TMUX_PANE")}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", POLARIS_TERMINAL="1", TERM_PROGRAM="Polaris")
    if not any(env.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LANG"] = "en_GB.UTF-8"       # launchd sets no locale, and tmux without one prints _ for every non-ASCII cell
    env["PATH"] = ":".join(p for p in [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", env.get("PATH", "")] if p)
    return env


def pty_preflight(kind: str, argv: list, env: dict) -> str:
    """Why this tab cannot start, in one line, or "". Checked before forking, so a missing
    program is a sentence in the tab rather than a pane that flashes and dies under tmux."""
    if not (os.path.isabs(argv[0]) and os.access(argv[0], os.X_OK)) and not shutil.which(argv[0], path=env["PATH"]):
        return f"{argv[0]}: command not found"
    if PTY_ARGV or kind != "session":
        return ""
    prog = session_cmd()[0]
    if not (os.path.isabs(prog) and os.access(prog, os.X_OK)) and not shutil.which(prog, path=env["PATH"]):
        return f"{prog}: not found on PATH ({env['PATH']}). Set POLARIS_SESSION_CMD."
    return ""


def tmux_dress(tmux: str, name: str) -> None:
    """Session options for a Polaris tab, once tmux has made the session: the wheel scrolls tmux's
    history (mouse on; Option-drag still selects natively), a quiet status line that names the
    session. Only this session's options; the server and your other sessions are untouched."""
    for _ in range(30):
        if subprocess.run([tmux, "has-session", "-t", "=" + name], capture_output=True, timeout=5).returncode == 0:
            break
        time.sleep(0.1)
    else:
        return
    for opt in (["mouse", "on"], ["status-style", "bg=#0b1016,fg=#6b7a8c"], ["status-left", "[#S] "], ["status-left-length", "40"],
                ["status-right", "detach: close the tab or Ctrl-b d"], ["status-right-length", "40"]):
        # set-option takes a pane target: a bare "=name" is "no such session", "=name:" is the session
        subprocess.run([tmux, "set-option", "-t", "=" + name + ":", *opt], capture_output=True, timeout=5)


class Pty:
    """One pseudo-terminal for one Terminal tab: a child on a pty (stdlib pty.fork), a reader
    thread into a ring buffer, and a cursor so the page long-polls only the bytes it has not
    seen. One child per tab: a second open while one runs answers 409."""

    def __init__(self, tab: str = "session", kind: str | None = None):
        self.tab = tab
        self.kind = kind or ("shell" if tab.startswith("shell") else "session")
        self.cond = PTY_COND
        self.lock = PTY_COND            # the condition is the lock: every pty shares it (see PTY_COND)
        self.fd = -1
        self.pid = 0
        self.buf = bytearray()
        self.base = 0                   # the cursor of buf[0]
        self.alive = False
        self.exit: int | None = None
        self.tmux = False
        self.argv: list = []
        self.opened = ""
        self.opened_at = 0.0
        self.ended = ""                 # why the child ended, in words, for the tab

    def view(self) -> dict:
        with self.lock:
            return self._view_locked()

    def _view_locked(self) -> dict:
        return {"id": self.tab, "kind": self.kind, "open": self.alive, "pid": self.pid or None, "tmux": self.tmux,
                "name": tmux_name(self.tab) if self.tmux else "", "argv": self.argv, "opened": self.opened,
                "exit": self.exit, "ended": self.ended, "cursor": self.base + len(self.buf)}

    def open(self, cols: int, rows: int) -> dict:
        import fcntl
        import pty as _pty
        import struct
        import termios
        with self.lock:
            if self.alive:
                return {"ok": False, "error": "this terminal is already open", "code": 409, **self._view_locked()}
            if PTY_ARGV:
                argv, tmux = list(PTY_ARGV), False
            else:
                argv, tmux = session_argv(kind=self.kind, name=tmux_name(self.tab))
            env = pty_env(cols, rows)
            why = pty_preflight(self.kind, argv, env)
            if why:
                self.ended, self.exit = why, 127
                return {"ok": False, "error": why, "code": 500, **self._view_locked()}
            winsz = struct.pack("HHHH", max(2, rows), max(2, cols), 0, 0)
            pid, fd = _pty.fork()
            if pid == 0:                          # the child: the window size first, then exec; never return into the server
                try:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, winsz)
                    os.chdir(str(config.repo()))
                    os.execvpe(argv[0], argv, env)
                except OSError as e:
                    os.write(2, f"cannot run {argv[0]}: {e}\r\n".encode())
                finally:
                    os._exit(127)
            self.fd, self.pid, self.alive, self.exit, self.tmux, self.argv = fd, pid, True, None, tmux, argv
            self.buf, self.base, self.ended = bytearray(), 0, ""
            self.opened = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            self.opened_at = time.time()
            self._resize_locked(cols, rows)
            threading.Thread(target=self._reader, args=(fd, pid), daemon=True, name=f"polaris-pty-{self.tab}").start()
            if tmux:
                threading.Thread(target=tmux_dress, args=(argv[0], tmux_name(self.tab)), daemon=True).start()
            return {"ok": True, **self._view_locked()}

    def _why_ended(self) -> str:
        """The child's end in words: the exit, and the last lines it printed when it died young
        (a tmux whose command failed, a shell that could not start)."""
        tail = ANSI.sub("", bytes(self.buf[-4096:]).decode("utf-8", "replace")).replace("\r", "\n")
        lines = [ln.strip() for ln in tail.split("\n") if ln.strip()]
        who = "tmux" if self.tmux else os.path.basename(str(self.argv[0])) if self.argv else "the terminal"
        young = time.time() - self.opened_at < 5
        if self.tmux and lines and lines[-1].startswith("[detached"):
            return "detached: the tmux session lives on"
        if self.tmux and lines and lines[-1] == "[exited]":
            lines = lines[:-1]
            if not young:
                return "the shell exited"
        said = " · ".join(lines[-2:])[:300] if young or self.exit else ""
        return f"{who} exited (exit {self.exit})" + (f": {said}" if said else "")

    def _reader(self, fd: int, pid: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            with self.lock:
                if fd != self.fd:
                    return                        # closed and reopened under us
                if not chunk:
                    self.alive = False
                    try:
                        _, status = os.waitpid(pid, 0)
                        self.exit = os.waitstatus_to_exitcode(status)
                    except (OSError, ValueError):
                        self.exit = -1
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self.fd = -1
                    self.ended = self._why_ended()
                    self.cond.notify_all()
                    return
                self.buf += chunk
                if len(self.buf) > PTY_KEEP:
                    drop = len(self.buf) - PTY_KEEP
                    del self.buf[:drop]
                    self.base += drop
                self.cond.notify_all()

    def write(self, data: bytes) -> dict:
        with self.lock:
            if not self.alive:
                return {"ok": False, "error": "this terminal is not open", "code": 409}
            fd = self.fd
        try:
            view = memoryview(data)
            while view:                            # a large paste can exceed the pty's buffer: write it all
                n = os.write(fd, view)
                view = view[n:]
        except OSError as e:
            return {"ok": False, "error": f"write failed: {e}", "code": 500}
        return {"ok": True, "n": len(data)}

    def _chunk_locked(self, cursor: int) -> dict:
        end = self.base + len(self.buf)
        cursor = min(cursor, end)
        lost = cursor < self.base
        start = max(cursor, self.base)
        data = bytes(self.buf[start - self.base:end - self.base])
        return {"ok": True, "cursor": end, "data": base64.b64encode(data).decode("ascii"), "open": self.alive,
                "exit": self.exit, "ended": self.ended, "lost": lost}

    def read(self, cursor: int, wait_s: float = 25.0) -> dict:
        """The bytes after `cursor`, waiting up to wait_s for some. A cursor before the kept window
        starts from the oldest byte kept and says so."""
        with self.lock:
            cursor = min(cursor, self.base + len(self.buf))
            if self.alive:
                self.cond.wait_for(lambda: self.base + len(self.buf) > cursor or not self.alive, timeout=wait_s)
            return self._chunk_locked(cursor)

    def _resize_locked(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, rows), max(2, cols), 0, 0))

    def resize(self, cols: int, rows: int) -> dict:
        with self.lock:
            if not self.alive:
                return {"ok": False, "error": "this terminal is not open", "code": 409}
            try:
                self._resize_locked(cols, rows)
                os.kill(self.pid, signal.SIGWINCH)
            except OSError as e:
                return {"ok": False, "error": f"resize failed: {e}", "code": 500}
            return {"ok": True, "cols": cols, "rows": rows}

    def close(self) -> dict:
        """End the child. Under tmux this is a detach (the session lives on); otherwise the
        process gets SIGHUP, as a closing terminal window would send."""
        with self.lock:
            if not self.alive:
                return {"ok": True, "open": False, "note": "nothing was open"}
            pid, fd = self.pid, self.fd
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        with self.lock:
            self.cond.wait_for(lambda: not self.alive, timeout=3.0)
            if self.alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                self.cond.wait_for(lambda: not self.alive, timeout=3.0)
            if self.tmux:
                self.ended = "detached: the tmux session lives on"
            return {"ok": True, "open": self.alive, "exit": self.exit, "tmux": self.tmux, "fd": fd, "ended": self.ended}


PTY = Pty("session")                     # the session tab
PTYS: dict = {"session": PTY}            # every tab by id: "session", "shell-1", "shell-2", ...
PTY_ROUTES = ("/api/pty/open", "/api/pty/write", "/api/pty/resize", "/api/pty/close")


def pty_tab(tab: str, create: bool = False):
    """The Pty for a tab id, made on first open; None for a bad id, or a new one past PTY_MAX."""
    tab = tab or "session"
    if not PTY_TAB.fullmatch(tab):
        return None
    with PTY_COND:
        p = PTYS.get(tab)
        if p is None and create:
            if sum(1 for q in PTYS.values() if q.alive) >= PTY_MAX:
                return None
            p = PTYS[tab] = Pty(tab)
        return p


def pty_tabs() -> list:
    """Every tab the desk knows, open or ended, shells first by number, the session last."""
    with PTY_COND:
        ps = list(PTYS.values())
    views = [p.view() for p in ps if p.alive or p.opened or p.ended]
    return sorted(views, key=lambda v: (v["kind"] == "session", int(v["id"].split("-")[1]) if "-" in v["id"] else 0))


def pty_reads(cursors: dict, wait_s: float = 25.0) -> dict:
    """One long-poll for every tab the page shows: {tab: cursor} in, {tab: chunk} out, as soon as
    any of them has output or ends. A tab the desk does not know answers open false, missing true."""
    def ready() -> bool:
        for tab, cur in cursors.items():
            p = PTYS.get(tab)
            if p is None or not p.alive or p.base + len(p.buf) > cur:
                return True
        return False
    with PTY_COND:
        if cursors and not ready():
            PTY_COND.wait_for(ready, timeout=wait_s)
        out = {}
        for tab, cur in cursors.items():
            p = PTYS.get(tab)
            out[tab] = p._chunk_locked(cur) if p is not None else {"ok": False, "open": False, "missing": True, "cursor": 0, "data": ""}
        return {"ok": True, "tabs": out}



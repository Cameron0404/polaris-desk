"""Where Polaris keeps its files, read from the environment so nothing is hard-coded.

    POLARIS_HOME         the data folder (default ~/.polaris). Holds questions/, commands/,
                         pins/, answers/, feeds/ and state/.
    POLARIS_REPO         the checkout commands and terminals start in (default: the current
                         directory when Polaris starts).
    POLARIS_BRANCH       the branch of POLARIS_REPO the Run these checker watches (default main).
                         A new commit there that touches a file a command names triggers a check.
    POLARIS_SHARED       checkouts other runs share, separated by ":". A branch switch or a
                         stash in one of them is judged destructive by the Run these checker.
    POLARIS_SECRETS_GLOB a path fragment that marks secret files (default "secrets."). Any
                         command naming it is judged destructive.
    POLARIS_TZ           an IANA time zone for "ask me later" (default: the Mac's own).
    POLARIS_SOURCES      optional dotted module path of your own data source (see sources.py).

Every module calls these functions at the moment it needs a path, so a test can point
POLARIS_HOME at a scratch folder and nothing real is touched.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("POLARIS_HOME") or Path.home() / ".polaris").expanduser()


def repo() -> Path:
    return Path(os.environ.get("POLARIS_REPO") or os.getcwd()).expanduser()


def branch() -> str:
    return os.environ.get("POLARIS_BRANCH") or "main"


def shared() -> set:
    raw = os.environ.get("POLARIS_SHARED") or ""
    out = set()
    for p in raw.split(":"):
        if p.strip():
            out.add(Path(os.path.realpath(os.path.expanduser(p.strip()))))
    return out


def secrets_marker() -> str:
    return os.environ.get("POLARIS_SECRETS_GLOB") or "secrets."


def tz():
    name = os.environ.get("POLARIS_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001  an unknown zone falls back to local time
            pass
    return dt.datetime.now().astimezone().tzinfo


def now() -> dt.datetime:
    return dt.datetime.now(tz())


def sub(name: str) -> Path:
    """A folder under POLARIS_HOME: questions, commands, pins, answers, feeds, state."""
    return home() / name


def state_file(name: str) -> Path:
    return home() / "state" / name

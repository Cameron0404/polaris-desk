#!/usr/bin/env python3
"""Run every self-test. Exit 0 only when all pass.

    python3 tests/run_all.py [--quiet]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITES = [
    ("pins", [sys.executable, "-m", "polaris.pins", "--selftest"]),
    ("questions", [sys.executable, "-m", "polaris.questions", "--selftest"]),
    ("attention", [sys.executable, "-m", "polaris.attention", "--selftest"]),
    ("run these", [sys.executable, "tests/run_these_selftest.py"]),
    ("server", [sys.executable, "tests/server_selftest.py"]),
]


def main() -> int:
    quiet = "--quiet" in sys.argv[1:]
    bad = []
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "POLARIS_HOME": td}
        for name, argv in SUITES:
            r = subprocess.run(argv, cwd=str(REPO), env=env, capture_output=True, text=True)
            lines = (r.stdout + r.stderr).strip().splitlines()
            if not quiet or r.returncode:
                print("\n".join(lines))
            print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {name}: {lines[-1] if lines else ''}")
            if r.returncode:
                bad.append(name)
    print(f"\n{len(SUITES) - len(bad)} of {len(SUITES)} suites passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

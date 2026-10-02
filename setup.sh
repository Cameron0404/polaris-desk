#!/bin/bash
# setup.sh: get Polaris ready on this Mac. Safe to run again.
#   ./setup.sh                 data folder with example data, xterm.js fetched, tests run
#   ./setup.sh --no-examples   an empty data folder instead (your own agents fill it)
#   ./setup.sh --native        also a .venv with pywebview, so Polaris opens in its own window
#   ./setup.sh --app           also build ~/Applications/Polaris.app (macOS)
# POLARIS_HOME (default ~/.polaris) is where the data folder goes.
set -euo pipefail
trap 'echo "setup: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${POLARIS_HOME:-$HOME/.polaris}"
EXAMPLES=1; NATIVE=0; APP=0
for a in "$@"; do
  case "$a" in
    --no-examples) EXAMPLES=0 ;;
    --native) NATIVE=1 ;;
    --app) APP=1 ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    *) echo "setup: unknown option $a" >&2; exit 2 ;;
  esac
done

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "setup: python3 is not on PATH" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "setup: Python 3.9 or later is needed" >&2; exit 1; }
echo "1. Python: $("$PY" --version)"

mkdir -p "$HOME_DIR"/{questions,commands,pins,answers,feeds,state,queue}
chmod 700 "$HOME_DIR"
if [ "$EXAMPLES" = 1 ] && [ -z "$(find "$HOME_DIR/questions" "$HOME_DIR/commands" "$HOME_DIR/feeds" -type f 2>/dev/null | head -1)" ]; then
  cp -R "$HERE/examples/home/." "$HOME_DIR/"
  echo "2. Data folder: $HOME_DIR (example data copied in, every item is marked Example)"
else
  echo "2. Data folder: $HOME_DIR (left as it is)"
fi

# xterm.js and its fit addon, from the npm registry, checked against known hashes. They are
# served from this Mac by Polaris itself, never loaded from a CDN by the page.
VENDOR="$HERE/web/vendor"
fetch() {  # fetch <npm package> <version> <file in tarball> <dest> <sha256>
  local pkg="$1" ver="$2" inner="$3" dest="$4" sum="$5"
  if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sum" ]; then return 0; fi
  local tmp; tmp="$(mktemp -d)"
  curl -fsSL "https://registry.npmjs.org/$pkg/-/$(basename "$pkg")-$ver.tgz" | tar xz -C "$tmp"
  mkdir -p "$(dirname "$dest")"
  cp "$tmp/package/$inner" "$dest"
  [ -f "$tmp/package/LICENSE" ] && cp "$tmp/package/LICENSE" "$(dirname "$dest")/LICENSE"
  rm -rf "$tmp"
  local got; got="$(shasum -a 256 "$dest" | cut -d' ' -f1)"
  [ "$got" = "$sum" ] || { rm -f "$dest"; echo "setup: $dest has the wrong hash ($got)" >&2; exit 1; }
}
fetch @xterm/xterm 5.5.0 lib/xterm.js "$VENDOR/xterm/xterm.js" 1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495  # sha256 of the published npm file
fetch @xterm/xterm 5.5.0 css/xterm.css "$VENDOR/xterm/xterm.css" ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6  # sha256 of the published npm file
fetch @xterm/addon-fit 0.10.0 lib/addon-fit.js "$VENDOR/addon-fit/addon-fit.js" bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089  # sha256 of the published npm file
echo "3. xterm.js 5.5.0 and addon-fit 0.10.0 in web/vendor (hashes checked)"

if [ "$NATIVE" = 1 ]; then
  "$PY" -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install --quiet --upgrade pip pywebview
  echo "4. .venv with pywebview: Polaris opens in its own window"
else
  echo "4. No native window (add --native for one); Polaris opens in your browser"
fi

if [ "$APP" = 1 ]; then
  bash "$HERE/scripts/make_polaris_app.sh"
fi

echo "5. Tests:"
POLARIS_HOME="$(mktemp -d)" "$PY" "$HERE/tests/run_all.py" --quiet
echo
echo "Ready. Start it with:  $HERE/bin/polaris"
[ "$HOME_DIR" = "$HOME/.polaris" ] || echo "and export POLARIS_HOME=\"$HOME_DIR\" first"

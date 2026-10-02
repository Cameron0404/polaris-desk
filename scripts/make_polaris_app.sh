#!/bin/bash
# make_polaris_app.sh: Polaris as a Mac app. Writes ~/Applications/Polaris.app (never
# /Applications), a minimal bundle whose executable runs this checkout's bin/polaris with
# POLARIS_APP=1, so the desk opens in its native pywebview window when the .venv has it, and a
# second launch focuses the running desk (the one-desk rule in polaris/server.py). The icon is
# drawn by stdlib Python into a PNG, sized by sips and packed by iconutil, so nothing is installed.
#   bash scripts/make_polaris_app.sh             -> ~/Applications/Polaris.app
#   bash scripts/make_polaris_app.sh /tmp/x.app  -> elsewhere (tests)
#   POLARIS_HOME=... POLARIS_REPO=... bash ...   -> baked into the app, so Finder starts it with them
#   open ~/Applications/Polaris.app
set -euo pipefail
# Name the line that failed, so an errexit stop never ends the build in silence.
trap 'echo "FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$HOME/Applications/Polaris.app}"
[ -x "$HERE/bin/polaris" ] || { echo "no bin/polaris in $HERE" >&2; exit 1; }
PHOME="${POLARIS_HOME:-$HOME/.polaris}"
PREPO="${POLARIS_REPO:-$HOME}"
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=python3

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/MacOS/Polaris" <<EOF
#!/bin/bash
# Polaris.app's executable, written by make_polaris_app.sh. Finder starts apps with almost no
# PATH, so one is set here (claude and tmux usually live in ~/.local/bin and /opt/homebrew/bin).
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export POLARIS_APP=1
export POLARIS_HOME="$PHOME"
export POLARIS_REPO="$PREPO"
export LANG="\${LANG:-en_GB.UTF-8}"
cd "$PREPO" 2>/dev/null || true
exec "$HERE/bin/polaris" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/Polaris"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Polaris</string>
  <key>CFBundleDisplayName</key><string>Polaris</string>
  <key>CFBundleIdentifier</key><string>local.polaris.desk</string>
  <key>CFBundleExecutable</key><string>Polaris</string>
  <key>CFBundleIconFile</key><string>Polaris</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>3</string>
  <key>CFBundleShortVersionString</key><string>3.0</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Polaris, MIT licence</string>
</dict>
</plist>
EOF
printf 'APPL????' > "$APP/Contents/PkgInfo"

# The icon: a near-black rounded square, a soft sky-blue glow and a four-point star (Polaris).
# Drawn pixel by pixel with zlib and struct, so it needs no library.
TMP="$(mktemp -d "${TMPDIR:-/tmp}/polaris-icon.XXXXXX")"
"$PY" - "$TMP/icon.png" <<'EOF'
import math, struct, sys, zlib
N = 1024
out = sys.argv[1]
rows = []
cx = cy = N / 2
rad = N * 0.225          # macOS icon corner radius
inset = N * 0.05         # the rounded square sits inside the canvas, as Apple's do
def inside(x, y):
    l, t, r, b = inset, inset, N - inset, N - inset
    if x < l or x > r or y < t or y > b:
        return False
    qx = max(l + rad - x, x - (r - rad), 0)
    qy = max(t + rad - y, y - (b - rad), 0)
    return qx * qx + qy * qy <= rad * rad
def star(x, y):
    # a four-point star: |dx|^0.5 + |dy|^0.5 <= s^0.5 gives the concave points
    dx, dy = abs(x - cx), abs(y - cy)
    s = N * 0.30
    v = math.sqrt(dx) + math.sqrt(dy) - math.sqrt(s)
    return max(0.0, min(1.0, -v / 2.5 + 0.5))
for y in range(N):
    row = bytearray([0])
    for x in range(N):
        if not inside(x + .5, y + .5):
            row += b"\x00\x00\x00\x00"
            continue
        d = math.hypot(x - cx, y - cy) / (N / 2)
        glow = math.exp(-(d / 0.42) ** 2)
        r, g, b = 10 + 30 * glow, 12 + 90 * glow, 13 + 140 * glow
        a = star(x, y)
        r, g, b = r + (232 - r) * a, g + (240 - g) * a, b + (255 - b) * a
        row += bytes((int(r), int(g), int(b), 255))
    rows.append(bytes(row))
raw = b"".join(rows)
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
open(out, "wb").write(png)
EOF

ICONSET="$TMP/Polaris.iconset"
mkdir -p "$ICONSET"
for n in 16 32 128 256 512; do
  sips -z $n $n "$TMP/icon.png" --out "$ICONSET/icon_${n}x${n}.png" >/dev/null
  d=$((n * 2))
  sips -z $d $d "$TMP/icon.png" --out "$ICONSET/icon_${n}x${n}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Polaris.icns"
cp "$TMP/icon.png" "$APP/Contents/Resources/Polaris.png"
rm -rf "$TMP"
touch "$APP"    # Finder and the Dock re-read the icon
# Spotlight and `open -b local.polaris.desk` learn the bundle from LaunchServices.
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 || true
echo "Polaris.app written: $APP"
echo "open \"$APP\"     (or Spotlight: Polaris)"

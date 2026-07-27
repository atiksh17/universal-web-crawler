#!/usr/bin/env bash
# Universal Crawler — native setup (no Docker). Debian/Ubuntu VPS or macOS.
# Usage:  bash setup.sh
set -euo pipefail

PYBIN="${PYBIN:-python3.12}"

echo ">> Checking Python 3.12 (nodriver breaks on 3.14)..."
if ! command -v "$PYBIN" >/dev/null; then
  echo "!! $PYBIN not found."
  echo "   Debian/Ubuntu: sudo apt-get install -y python3.12 python3.12-venv"
  echo "   macOS:         brew install python@3.12"
  exit 1
fi

echo ">> Creating venv + installing pinned deps..."
"$PYBIN" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

echo ">> Locating Chrome/Chromium..."
# Prefer standalone google-chrome-stable. On Ubuntu, `apt install chromium` installs the
# SNAP, whose slow cold start exceeds nodriver's ~3s connect window under concurrency
# ("Failed to connect to browser", cold-only — warm works). See README.
CHROME=""
for c in google-chrome-stable google-chrome chromium chromium-browser; do
  if command -v "$c" >/dev/null; then CHROME="$(command -v "$c")"; break; fi
done
if [ -z "$CHROME" ] && command -v apt-get >/dev/null; then
  echo "   Installing google-chrome-stable (needs sudo)..."
  TMPDEB="$(mktemp -d)/chrome.deb"
  if curl -fsSL -o "$TMPDEB" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb; then
    sudo apt-get update && sudo apt-get install -y "$TMPDEB" || true
    CHROME="$(command -v google-chrome-stable || true)"
  fi
  rm -f "$TMPDEB"
fi
case "$CHROME" in
  /snap/*) echo "   !! WARNING: that is the SNAP chromium — L2 will fail on cold starts."
           echo "      Install standalone Chrome and set CRAWLER_CHROME_PATH to it." ;;
esac

[ -f .env ] || cp .env.example .env

echo ""
echo "============================================================"
echo "Setup done."
if [ -n "$CHROME" ]; then
  echo "Chromium: $CHROME"
  echo "  -> ensure .env has: CRAWLER_CHROME_PATH=$CHROME"
else
  echo "Chromium NOT found. Install it, then set CRAWLER_CHROME_PATH in .env."
fi
echo ""
echo "Next:"
echo "  1) edit .env  (set tiers; add Web Unlocker key/zone for L4)"
echo "  2) test:  .venv/bin/python smoke.py"
echo "  3) run:   .venv/bin/uvicorn app.api:app --host 0.0.0.0 --port 8000"
echo "============================================================"

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

echo ">> Locating Chromium..."
CHROME=""
if command -v chromium >/dev/null; then CHROME="$(command -v chromium)";
elif command -v chromium-browser >/dev/null; then CHROME="$(command -v chromium-browser)";
else
  echo "   Installing chromium (needs sudo)..."
  if command -v apt-get >/dev/null; then
    sudo apt-get update && (sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser) || true
    CHROME="$(command -v chromium || command -v chromium-browser || true)"
  fi
fi

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

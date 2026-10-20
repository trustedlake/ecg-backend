#!/usr/bin/env bash
# Builds the Expo app web export (ecg-reader repo) into frontend_dist/, served by Django (config/spa.py).
# Usage: scripts/build_frontend.sh <path to ecg-reader>
# The web build talks to the same origin: EXPO_PUBLIC_API_BASE_URL is empty on purpose.
set -euo pipefail

FRONT_DIR="${1:?usage: scripts/build_frontend.sh <path to ecg-reader>}"
BACK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$BACK_DIR/frontend_dist"

cd "$FRONT_DIR"
npm ci --no-audit --no-fund
cd apps/mobile
rm -rf dist-web
EXPO_PUBLIC_API_BASE_URL="" CI=1 npx expo export --platform web --output-dir dist-web

test -f dist-web/index.html || { echo "export has no index.html" >&2; exit 1; }
rm -rf "$OUT_DIR"
cp -R dist-web "$OUT_DIR"
echo "frontend_dist pronto: $(find "$OUT_DIR" -type f | wc -l | tr -d ' ') arquivos"

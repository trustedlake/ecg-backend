#!/usr/bin/env bash
# Gera o export web do app Expo (repo leitor-ecg) em frontend_dist/, servido pelo Django (config/spa.py).
# Uso: scripts/build_frontend.sh <caminho do leitor-ecg>
# O build web fala com a mesma origem: EXPO_PUBLIC_API_BASE_URL fica vazio de propósito.
set -euo pipefail

FRONT_DIR="${1:?uso: scripts/build_frontend.sh <caminho do leitor-ecg>}"
BACK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$BACK_DIR/frontend_dist"

cd "$FRONT_DIR"
npm ci --no-audit --no-fund
cd apps/mobile
rm -rf dist-web
EXPO_PUBLIC_API_BASE_URL="" CI=1 npx expo export --platform web --output-dir dist-web

test -f dist-web/index.html || { echo "export sem index.html" >&2; exit 1; }
rm -rf "$OUT_DIR"
cp -R dist-web "$OUT_DIR"
echo "frontend_dist pronto: $(find "$OUT_DIR" -type f | wc -l | tr -d ' ') arquivos"

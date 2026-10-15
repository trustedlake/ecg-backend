#!/usr/bin/env bash
# Monta os pacotes ZIP das lambdas mock em mock_lambdas/build/<lambda>/.
# Pillow é baixado como wheel Linux arm64 / CPython 3.12 (runtime da Lambda),
# então funciona a partir do macOS sem Docker. boto3 já vem no runtime.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
rm -rf build
for fn in reading interpretation; do
  mkdir -p "build/$fn"
  cp "$fn/handler.py" "build/$fn/"
  cp -R ecg_contract "build/$fn/"
done
"$PYTHON" -m pip install --quiet --target build/reading \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: "pillow==12.*"
find build -name '__pycache__' -prune -exec rm -rf {} +
echo "ok: $(du -sh build/reading | cut -f1) reading, $(du -sh build/interpretation | cut -f1) interpretation"

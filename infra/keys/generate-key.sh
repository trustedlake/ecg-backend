#!/usr/bin/env bash
# Generates the SSH key pair used by ecg-backend (idempotent).
# Run ONCE, before the first `terraform apply`:
#   ./infra/keys/generate-key.sh
# Cria infra/keys/ecg-backend.pem (privada) e infra/keys/ecg-backend.pub
# (public, the only one Terraform registers in aws_key_pair). The private one must NOT
# leave the machine or go into git (infra/keys/ is in .gitignore).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/ecg-backend.pem"

if [[ -f "$OUT" && -f "$DIR/ecg-backend.pub" ]]; then
  echo "Key already exists: $OUT (nothing to do)."
  exit 0
fi

# Partial (only the .pub or only the .pem left): regenerate from scratch.
rm -f "$OUT" "$DIR/ecg-backend.pub" "$OUT.pub"

ssh-keygen -t ed25519 -f "$OUT" -N "" -C "ecg-backend-deploy"
mv "$OUT.pub" "$DIR/ecg-backend.pub"
chmod 600 "$OUT"
echo "Par gerado: $OUT"
echo "Make sure $OUT is outside git (infra/keys/ is ignored)."
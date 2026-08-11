#!/usr/bin/env bash
# Gera o par de chaves SSH usado pelo ecg-backend (idempotente).
# Roda UMA vez, antes do primeiro `terraform apply`:
#   ./infra/keys/generate-key.sh
# Cria infra/keys/ecg-backend.pem (privada) e infra/keys/ecg-backend.pub
# (publica, a unica que o Terraform registra no aws_key_pair). A privada NAO
# deve sair da maquina nem ir pro git (infra/keys/ esta no .gitignore).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/ecg-backend.pem"

if [[ -f "$OUT" && -f "$DIR/ecg-backend.pub" ]]; then
  echo "Chave ja existe: $OUT (nada a fazer)."
  exit 0
fi

# Parcial (so o .pub sobrou, ou so o .pem): regenera limpo.
rm -f "$OUT" "$DIR/ecg-backend.pub" "$OUT.pub"

ssh-keygen -t ed25519 -f "$OUT" -N "" -C "ecg-backend-deploy"
mv "$OUT.pub" "$DIR/ecg-backend.pub"
chmod 600 "$OUT"
echo "Par gerado: $OUT"
echo "Confirme que $OUT esta fora do git (infra/keys/ ignorado)."
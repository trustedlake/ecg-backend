#!/usr/bin/env bash
# Deploy manual do ecg-backend para a instância EC2.
# Requer: SSH key do terraform output (infra/keys/ecg-backend.pem) e o IP do output instance_public_ip.
# Roda do diretório ecg-backend/ (raiz do projeto Django).
#
# Uso:
#   ./infra/deploy.sh <IP_DO_INSTANCE_PUBLIC_IP>
# No Windows: rodar via git-bash ou WSL (sem rsync? usa tar+ssh).
set -euo pipefail

INSTANCE_IP="${1:?Uso: deploy.sh <IP_DO_EC2>}"
KEY="$(dirname "$0")/keys/ecg-backend.pem"
REMOTE_USER="ecgapp"
APP_DIR="/home/ecgapp/ecg-backend"

[[ -f "$KEY" ]] || { echo "ERRO: $KEY nao existe (rode ./infra/keys/generate-key.sh antes do primeiro deploy)."; exit 1; }

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
SCP=(scp -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

echo "==> 1/5 Sincronizando código para ${INSTANCE_IP}"
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete -e "ssh -i \"$KEY\" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
    --exclude .venv --exclude .env --exclude .git --exclude __pycache__ \
    --exclude '*.pyc' --exclude db.sqlite3 --exclude smoke_e2e.py \
    ./ "${REMOTE_USER}@${INSTANCE_IP}:${APP_DIR}/"
else
  # Fallback sem rsync (Windows sem extra): tar + ssh.
  tar czf - --exclude=.venv --exclude=.env --exclude=.git --exclude=__pycache__ \
    --exclude='*.pyc' --exclude=db.sqlite3 --exclude=smoke_e2e.py . \
    | "${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
      "mkdir -p ${APP_DIR} && tar xzf - -C ${APP_DIR}"
fi

echo "==> 2/5 Criando/atualizando .venv e instalando deps"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
  "cd ${APP_DIR} && python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"

echo "==> 3/5 Migrações"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
  "cd ${APP_DIR} && .venv/bin/python manage.py migrate"

echo "==> 4/5 Reiniciando serviço"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" "sudo systemctl restart ecg-backend"

echo "==> 5/5 Health check"
sleep 3
curl -sf -o /dev/null -X POST http://${INSTANCE_IP}:8000/api/auth/login/ -H 'Content-Type: application/json' -d '{}' \
  && echo "API no ar: http://${INSTANCE_IP}:8000" || { echo "Falha no health check — confira: ssh -i ${KEY} ${REMOTE_USER}@${INSTANCE_IP} 'sudo journalctl -u ecg-backend -n 50'"; exit 1; }

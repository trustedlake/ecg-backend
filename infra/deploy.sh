#!/usr/bin/env bash
# Manual ecg-backend deploy to the EC2 instance.
# Requer: SSH key do terraform output (infra/keys/ecg-backend.pem) e o IP do output instance_public_ip.
# Runs from the ecg-backend/ directory (Django project root).
#
# Uso:
#   ./infra/deploy.sh <IP_DO_INSTANCE_PUBLIC_IP>
# On Windows: run through git-bash or WSL (no rsync? uses tar+ssh).
set -euo pipefail

INSTANCE_IP="${1:?Uso: deploy.sh <IP_DO_EC2>}"
KEY="$(dirname "$0")/keys/ecg-backend.pem"
REMOTE_USER="ecgapp"
APP_DIR="/home/ecgapp/ecg-backend"

[[ -f "$KEY" ]] || { echo "ERROR: $KEY does not exist (run ./infra/keys/generate-key.sh before the first deploy)."; exit 1; }

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
SCP=(scp -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

echo "==> 1/5 Syncing code to ${INSTANCE_IP}"
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete -e "ssh -i \"$KEY\" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
    --exclude .venv --exclude .env --exclude .git --exclude __pycache__ \
    --exclude '*.pyc' --exclude db.sqlite3 --exclude smoke_e2e.py \
    ./ "${REMOTE_USER}@${INSTANCE_IP}:${APP_DIR}/"
else
  # Fallback without rsync (plain Windows): tar + ssh.
  tar czf - --exclude=.venv --exclude=.env --exclude=.git --exclude=__pycache__ \
    --exclude='*.pyc' --exclude=db.sqlite3 --exclude=smoke_e2e.py . \
    | "${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
      "mkdir -p ${APP_DIR} && tar xzf - -C ${APP_DIR}"
fi

echo "==> 2/5 Criando/atualizando .venv e instalando deps"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
  "cd ${APP_DIR} && python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"

echo "==> 3/5 Migrations"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" \
  "cd ${APP_DIR} && .venv/bin/python manage.py migrate"

echo "==> 4/5 Restarting service"
"${SSH[@]}" "${REMOTE_USER}@${INSTANCE_IP}" "sudo systemctl restart ecg-backend"

echo "==> 5/5 Health check"
sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://${INSTANCE_IP}:8000/api/auth/login/ -H 'Content-Type: application/json' -d '{}')
if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "400" ]]; then
  echo "API no ar: http://${INSTANCE_IP}:8000 (HTTP $HTTP_CODE)"
else
  echo "Health check failed (HTTP ${HTTP_CODE:-no response}); check: ssh -i ${KEY} ${REMOTE_USER}@${INSTANCE_IP} 'sudo journalctl -u ecg-backend -n 50'"
  exit 1
fi

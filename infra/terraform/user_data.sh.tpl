#!/bin/bash
# user_data do ecg-backend (primeiro boot — AL2023 arm64)
# Busca DATABASE_URL e DJANGO_SECRET_KEY do SSM Parameter Store (SecureString),
# cria o .env do app e registra o serviço systemd que o deploy.sh usará.
set -euxo pipefail

exec > /var/log/ecg-user-data.log 2>&1

APP_DIR="${app_dir}"
REGION="${aws_region}"
SSM_DB="${ssm_db_url}"
SSM_SECRET="${ssm_secret_key}"

# Usuário de app (sem senha, acesso via SSH com o key pair)
if ! id ecgapp >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash ecgapp
fi
echo 'ecgapp ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ecgapp
chmod 440 /etc/sudoers.d/ecgapp

# Propaga pro ecgapp a chave SSH que o cloud-init ja injetou no ec2-user
# (usuário padrão da AMI). Sem isso, o deploy.sh conectando como ecgapp
# tomaria permission denied.
mkdir -p /home/ecgapp/.ssh
cp /home/ec2-user/.ssh/authorized_keys /home/ecgapp/.ssh/authorized_keys
chown -R ecgapp:ecgapp /home/ecgapp/.ssh
chmod 700 /home/ecgapp/.ssh
chmod 600 /home/ecgapp/.ssh/authorized_keys

# Tooling
dnf install -y python3.12 python3.12-pip git

# Descobre o IP público da instância (IMDSv2) e monta o .env.
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-ipv4)

DATABASE_URL=$(aws ssm get-parameter --name "$SSM_DB" --with-decryption \
  --query Parameter.Value --output text --region "$REGION")
DJANGO_SECRET_KEY=$(aws ssm get-parameter --name "$SSM_SECRET" --with-decryption \
  --query Parameter.Value --output text --region "$REGION")

mkdir -p "${app_dir}"

# .env do Django (DEBUG=False, ALLOWED_HOSTS com o IP público de boot).
cat > "${app_dir}/.env" <<EOF
DEBUG=False
ALLOWED_HOSTS=$${PUBLIC_IP}
DATABASE_URL=$${DATABASE_URL}
DJANGO_SECRET_KEY=$${DJANGO_SECRET_KEY}
AWS_REGION=$${REGION}
EOF

chown -R ecgapp:ecgapp "${app_dir}"

# Serviço systemd — o deploy.sh cria .venv/instala deps e dá start.
cat > /etc/systemd/system/ecg-backend.service <<'UNIT'
[Unit]
Description=ecg-backend (Django via Gunicorn)
After=network-online.target
Wants=network-online.target

[Service]
User=ecgapp
Group=ecgapp
WorkingDirectory=/home/ecgapp/ecg-backend
EnvironmentFile=/home/ecgapp/ecg-backend/.env
# Timeout acima de 300s: lambdas da Fase B podem demorar mais que o default do gunicorn
ExecStart=/home/ecgapp/ecg-backend/.venv/bin/gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 --timeout 960 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# Não dá start no primeiro boot: o deploy.sh sincroniza o código e instala as deps antes.
systemctl enable ecg-backend.service

echo "user_data concluído"
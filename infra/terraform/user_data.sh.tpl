#!/bin/bash
# user_data do ecg-backend (primeiro boot — AL2023 arm64)
# Busca DATABASE_URL e DJANGO_SECRET_KEY do SSM Parameter Store (SecureString),
# creates the app .env and registers the systemd service that deploy.sh will use.
set -euxo pipefail

exec > /var/log/ecg-user-data.log 2>&1

APP_DIR="${app_dir}"
REGION="${aws_region}"
SSM_DB="${ssm_db_url}"
SSM_SECRET="${ssm_secret_key}"

# App user (no password, SSH access with the key pair)
if ! id ecgapp >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash ecgapp
fi
echo 'ecgapp ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ecgapp
chmod 440 /etc/sudoers.d/ecgapp

# Copies to ecgapp the SSH key cloud-init already injected into ec2-user
# (the AMI default user). Without this, deploy.sh connecting as ecgapp
# tomaria permission denied.
mkdir -p /home/ecgapp/.ssh
cp /home/ec2-user/.ssh/authorized_keys /home/ecgapp/.ssh/authorized_keys
chown -R ecgapp:ecgapp /home/ecgapp/.ssh
chmod 700 /home/ecgapp/.ssh
chmod 600 /home/ecgapp/.ssh/authorized_keys

# Tooling
dnf install -y python3.12 python3.12-pip git

# Finds the instance public IP (IMDSv2) and builds the .env.
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-ipv4)

DATABASE_URL=$(aws ssm get-parameter --name "$SSM_DB" --with-decryption \
  --query Parameter.Value --output text --region "$REGION")
DJANGO_SECRET_KEY=$(aws ssm get-parameter --name "$SSM_SECRET" --with-decryption \
  --query Parameter.Value --output text --region "$REGION")

mkdir -p "${app_dir}"

# Django .env (DEBUG=False, ALLOWED_HOSTS with the boot public IP).
cat > "${app_dir}/.env" <<EOF
DEBUG=False
ALLOWED_HOSTS=$${PUBLIC_IP}
DATABASE_URL=$${DATABASE_URL}
DJANGO_SECRET_KEY=$${DJANGO_SECRET_KEY}
AWS_REGION=$${REGION}
EOF

chown -R ecgapp:ecgapp "${app_dir}"

# systemd service: deploy.sh creates .venv, installs deps and starts it.
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
# Timeout above 300s: Phase B lambdas can take longer than the gunicorn default
ExecStart=/home/ecgapp/ecg-backend/.venv/bin/gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 --timeout 960 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# Not started on first boot: deploy.sh syncs the code and installs deps first.
systemctl enable ecg-backend.service

echo "user_data done"
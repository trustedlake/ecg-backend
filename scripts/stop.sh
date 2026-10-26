#!/usr/bin/env bash
# Turns production off: ECS to 0, then stops RDS (storage is still billed).
# RDS restarts on its own after 7 days stopped (AWS rule) and can be started manually.
# Usage: scripts/stop.sh   (default AWS_PROFILE: trustedlake)
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-trustedlake}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
DB_ID="${DB_ID:-ecg}"
CLUSTER="${CLUSTER:-ecg}"
SERVICE="${SERVICE:-ecg-backend}"

aws application-autoscaling register-scalable-target \
  --service-namespace ecs \
  --scalable-dimension ecs:service:DesiredCount \
  --resource-id "service/$CLUSTER/$SERVICE" \
  --min-capacity 0 --max-capacity 0
echo "ECS: min 0 / max 0. Esperando as tasks pararem..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"

status=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].DBInstanceStatus' --output text)
echo "RDS $DB_ID: $status"
if [ "$status" = "available" ]; then
  aws rds stop-db-instance --db-instance-identifier "$DB_ID" >/dev/null
  echo "RDS parando..."
  # The CLI has no "stopped" waiter: poll every 30 s (up to ~20 min).
  for _ in $(seq 1 40); do
    status=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
      --query 'DBInstances[0].DBInstanceStatus' --output text)
    [ "$status" = "stopped" ] && break
    sleep 30
  done
  echo "RDS $DB_ID: $status"
fi
echo "Desligado."

#!/usr/bin/env bash
# Turns production on outside business hours: RDS, then ECS (min 1 / max 2).
# Lasts until the next scheduled action (at 20:00 on business days ECS goes back to 0).
# Usage: scripts/start.sh   (default AWS_PROFILE: trustedlake)
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-trustedlake}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
DB_ID="${DB_ID:-ecg}"
CLUSTER="${CLUSTER:-ecg}"
SERVICE="${SERVICE:-ecg-backend}"

status=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].DBInstanceStatus' --output text)
echo "RDS $DB_ID: $status"
if [ "$status" = "stopped" ]; then
  aws rds start-db-instance --db-instance-identifier "$DB_ID" >/dev/null
  echo "RDS iniciando..."
fi
if [ "$status" != "available" ]; then
  aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
fi
echo "RDS available."

aws application-autoscaling register-scalable-target \
  --service-namespace ecs \
  --scalable-dimension ecs:service:DesiredCount \
  --resource-id "service/$CLUSTER/$SERVICE" \
  --min-capacity 1 --max-capacity 2
echo "ECS: min 1 / max 2. Waiting for the service to stabilize..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
echo "Ligado."

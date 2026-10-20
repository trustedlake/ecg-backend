#!/usr/bin/env bash
# Liga a produção fora do horário comercial: RDS e depois o ECS (min 1 / max 2).
# Vale até a próxima ação agendada (às 20:00 de dia útil o ECS volta a 0).
# Uso: scripts/ligar.sh   (AWS_PROFILE padrão: trustedlake)
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-trustedlake}"
export AWS_REGION="${AWS_REGION:-sa-east-1}"
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
echo "RDS disponível."

aws application-autoscaling register-scalable-target \
  --service-namespace ecs \
  --scalable-dimension ecs:service:DesiredCount \
  --resource-id "service/$CLUSTER/$SERVICE" \
  --min-capacity 1 --max-capacity 2
echo "ECS: min 1 / max 2. Esperando o serviço estabilizar..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
echo "Ligado."

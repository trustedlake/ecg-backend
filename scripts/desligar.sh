#!/usr/bin/env bash
# Desliga a produção: ECS em 0 e depois para o RDS (o disco continua cobrado).
# A RDS volta sozinha após 7 dias parada (regra da AWS) e o Scheduler a liga no próximo dia útil.
# Uso: scripts/desligar.sh   (AWS_PROFILE padrão: trustedlake)
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-trustedlake}"
export AWS_REGION="${AWS_REGION:-sa-east-1}"
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
  # A CLI não tem waiter de "stopped": consulta a cada 30 s (até ~20 min).
  for _ in $(seq 1 40); do
    status=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
      --query 'DBInstances[0].DBInstanceStatus' --output text)
    [ "$status" = "stopped" ] && break
    sleep 30
  done
  echo "RDS $DB_ID: $status"
fi
echo "Desligado."

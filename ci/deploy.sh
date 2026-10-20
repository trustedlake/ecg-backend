#!/usr/bin/env bash
# Deploy do backend ECG (etapa Deploy do CodePipeline; plano §I3):
#   1. liga o RDS se estiver parado (deploy fora do horário);
#   2. registra uma revisão nova da task definition com a imagem IMAGE_TAG;
#   3. migrate e ensure_admin via run-task (falha = deploy abortado, serviço intocado);
#   4. update-service e espera estabilizar (o circuit breaker volta sozinho se as tasks não sobem);
#   5. smoke em /api/health/ (versão nova) e / (HTML); se falhar, volta para a revisão anterior;
#   6. se o ambiente estava desligado e estamos fora do horário, desliga de novo.
#
# Variáveis (vêm do projeto CodeBuild, infra/aws/cicd.tf): IMAGE_TAG, ECR_REPOSITORY_URL, API_URL,
# CLUSTER, SERVICE, TASK_FAMILY, DB_ID, LOG_GROUP, OFFICE_HOURS_ENABLED.
set -euo pipefail

: "${IMAGE_TAG:?}" "${ECR_REPOSITORY_URL:?}" "${API_URL:?}"
CLUSTER="${CLUSTER:-ecg}"
SERVICE="${SERVICE:-ecg-backend}"
TASK_FAMILY="${TASK_FAMILY:-ecg-backend}"
DB_ID="${DB_ID:-ecg}"
LOG_GROUP="${LOG_GROUP:-/ecs/ecg-backend}"
OFFICE_HOURS_ENABLED="${OFFICE_HOURS_ENABLED:-true}"
API_URL="${API_URL%/}"
IMAGE="$ECR_REPOSITORY_URL:$IMAGE_TAG"
RESOURCE_ID="service/$CLUSTER/$SERVICE"

log() { echo "[deploy] $*"; }

inside_office_hours() {
  local dow hour
  dow=$(TZ=America/Sao_Paulo date +%u)
  hour=$(TZ=America/Sao_Paulo date +%H)
  [ "$dow" -le 5 ] && [ "$((10#$hour))" -ge 8 ] && [ "$((10#$hour))" -lt 20 ]
}

# --- 1. Ambiente ligado? ---
db_status=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].DBInstanceStatus' --output text)
min_capacity=$(aws application-autoscaling describe-scalable-targets --service-namespace ecs \
  --resource-ids "$RESOURCE_ID" --query 'ScalableTargets[0].MinCapacity' --output text)
was_off=false
if [ "$db_status" != "available" ] || [ "$min_capacity" = "0" ]; then
  was_off=true
  log "Ambiente desligado (RDS: $db_status, mínimo de tasks: $min_capacity); ligando para o deploy."
fi
if [ "$db_status" = "stopped" ]; then
  aws rds start-db-instance --db-instance-identifier "$DB_ID" >/dev/null
fi
if [ "$db_status" != "available" ]; then
  aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
  log "RDS disponível."
fi

# --- 2. Nova revisão da task definition ---
service_json=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0]')
previous_td=$(jq -r '.taskDefinition' <<<"$service_json")
network_config=$(jq -c '{awsvpcConfiguration: .networkConfiguration.awsvpcConfiguration}' <<<"$service_json")
previous_image=$(aws ecs describe-task-definition --task-definition "$previous_td" \
  --query "taskDefinition.containerDefinitions[?name=='web'].image | [0]" --output text)

# Base = última revisão da família: inclui mudanças do Terraform (CPU, envs) feitas depois do último deploy.
aws ecs describe-task-definition --task-definition "$TASK_FAMILY" --query taskDefinition \
  | jq --arg image "$IMAGE" --arg version "$IMAGE_TAG" '
      del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
      | .containerDefinitions |= map(
          if .name == "web" or .name == "worker" then
            .image = $image
            | .environment = ([.environment[] | select(.name != "APP_VERSION")]
                              + [{name: "APP_VERSION", value: $version}])
          else . end)' >/tmp/taskdef.json
new_td=$(aws ecs register-task-definition --cli-input-json file:///tmp/taskdef.json \
  --query taskDefinition.taskDefinitionArn --output text)
log "Revisão nova: $new_td (anterior: $previous_td, imagem anterior: $previous_image)"

# --- 3. migrate e ensure_admin em tasks avulsas ---
run_one_off() {
  local label="$1"; shift
  local cmd_json overrides task_arn task_id exit_code
  cmd_json=$(jq -cn '$ARGS.positional' --args "$@")
  # O worker é essencial: fica parado (signal.pause) até o web terminar e a task parar.
  overrides=$(jq -cn --argjson cmd "$cmd_json" '{containerOverrides: [
    {name: "web", command: $cmd},
    {name: "worker", command: ["python", "-c", "import signal; signal.pause()"]}]}')
  task_arn=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$new_td" --launch-type FARGATE \
    --network-configuration "$network_config" --overrides "$overrides" --started-by "deploy-$label" \
    --query 'tasks[0].taskArn' --output text)
  task_id="${task_arn##*/}"
  log "$label: task $task_id"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task_arn"
  exit_code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
    --query "tasks[0].containers[?name=='web'].exitCode | [0]" --output text)
  aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "ecg/web/$task_id" \
    --limit 40 --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' || true
  if [ "$exit_code" != "0" ]; then
    aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
      --query 'tasks[0].{stopCode: stopCode, reason: stoppedReason}' --output text
    log "$label falhou (exit $exit_code). Serviço não foi alterado."
    exit 1
  fi
  log "$label OK."
}

run_one_off migrate python manage.py migrate --noinput
run_one_off ensure_admin python manage.py ensure_admin

# --- 4. Atualiza o serviço ---
rollback() {
  if [[ "$previous_image" == *":bootstrap" ]]; then
    log "Primeiro deploy: não há revisão boa anterior para voltar."
    exit 1
  fi
  log "Voltando para $previous_td..."
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$previous_td" >/dev/null
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
  log "Rollback concluído."
  exit 1
}

if [ "$min_capacity" = "0" ]; then
  aws application-autoscaling register-scalable-target --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$RESOURCE_ID" \
    --min-capacity 1 --max-capacity 2
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$new_td" \
    --desired-count 1 >/dev/null
else
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$new_td" >/dev/null
fi
log "Esperando o serviço estabilizar..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"

# Estável também vale para "o circuit breaker voltou para a revisão antiga": confere qual ficou.
primary_td=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query "services[0].deployments[?status=='PRIMARY'] | [0].taskDefinition" --output text)
rollout=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query "services[0].deployments[?status=='PRIMARY'] | [0].rolloutState" --output text)
if [ "$primary_td" != "$new_td" ] || [ "$rollout" != "COMPLETED" ]; then
  log "Deploy não ficou na revisão nova (primária: $primary_td, $rollout). O circuit breaker reverteu."
  exit 1
fi

# --- 5. Smoke pela URL pública ---
smoke_ok=false
for _ in $(seq 1 10); do
  version=$(curl -fsS --max-time 10 "$API_URL/api/health/" | jq -r '.version' 2>/dev/null || true)
  if [ "$version" = "$IMAGE_TAG" ]; then
    smoke_ok=true
    break
  fi
  log "health ainda responde versão '${version:-?}'; nova tentativa em 6 s"
  sleep 6
done
if $smoke_ok && curl -fsS --max-time 10 "$API_URL/" | grep -qi "<html"; then
  log "Smoke OK: versão $IMAGE_TAG no ar em $API_URL"
else
  log "Smoke falhou."
  rollback
fi

# --- 6. Volta a desligar se o deploy foi fora do horário ---
if $was_off && [ "$OFFICE_HOURS_ENABLED" = "true" ] && ! inside_office_hours; then
  log "Fora do horário comercial: desligando de novo (ECS em 0, RDS parando)."
  aws application-autoscaling register-scalable-target --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$RESOURCE_ID" \
    --min-capacity 0 --max-capacity 0
  aws rds stop-db-instance --db-instance-identifier "$DB_ID" >/dev/null || true
fi
log "Deploy concluído."

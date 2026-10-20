#!/usr/bin/env bash
# Deploy do backend ECG (etapa Deploy do CodePipeline; plano §I3):
#   1. starts RDS if it is stopped (deploy outside business hours);
#   2. registers a new task definition revision with the IMAGE_TAG image;
#   3. migrate and ensure_admin via run-task (failure = deploy aborted, service untouched);
#   4. update-service and wait for it to stabilize (the circuit breaker rolls back if tasks do not start);
#   5. smoke test on /api/health/ (new version) and / (HTML); on failure, roll back to the previous revision;
#   6. if the environment was off and we are outside business hours, turn it off again.
#
# Variables (from the CodeBuild project, infra/aws/cicd.tf): IMAGE_TAG, ECR_REPOSITORY_URL, API_URL,
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
  log "Environment off (RDS: $db_status, minimum tasks: $min_capacity); starting it for the deploy."
fi
if [ "$db_status" = "stopped" ]; then
  aws rds start-db-instance --db-instance-identifier "$DB_ID" >/dev/null
fi
if [ "$db_status" != "available" ]; then
  aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
  log "RDS available."
fi

# --- 2. New task definition revision ---
service_json=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0]')
previous_td=$(jq -r '.taskDefinition' <<<"$service_json")
network_config=$(jq -c '{awsvpcConfiguration: .networkConfiguration.awsvpcConfiguration}' <<<"$service_json")
previous_image=$(aws ecs describe-task-definition --task-definition "$previous_td" \
  --query "taskDefinition.containerDefinitions[?name=='web'].image | [0]" --output text)

# Base = latest revision of the family: includes Terraform changes (CPU, envs) made after the last deploy.
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
log "New revision: $new_td (previous: $previous_td, previous image: $previous_image)"

# --- 3. migrate and ensure_admin in one-off tasks ---
run_one_off() {
  local label="$1"; shift
  local cmd_json overrides task_arn task_id exit_code
  # Via stdin: with --args, jq still reads an argument like "--noinput" as its own option.
  cmd_json=$(printf '%s\n' "$@" | jq -R . | jq -sc .)
  # The worker is essential: it stays idle (signal.pause) until web finishes and the task stops.
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
    log "$label failed (exit $exit_code). The service was not changed."
    exit 1
  fi
  log "$label OK."
}

run_one_off migrate python manage.py migrate --noinput
run_one_off ensure_admin python manage.py ensure_admin

# --- 4. Update the service ---
rollback() {
  if [[ "$previous_image" == *":bootstrap" ]]; then
    log "First deploy: there is no previous good revision to roll back to."
    exit 1
  fi
  log "Rolling back to $previous_td..."
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$previous_td" >/dev/null
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
  log "Rollback done."
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
log "Waiting for the service to stabilize..."
# services-stable can return with the rollout still IN_PROGRESS (seen on the first deploy, with the bootstrap revision still trying to start);
# wait for the primary revision rollout to finish (up to 20 min), then check which revision stuck.
for _ in $(seq 1 120); do
  read -r primary_td rollout < <(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query "services[0].deployments[?status=='PRIMARY'] | [0].[taskDefinition, rolloutState]" --output text)
  [ "$rollout" != "IN_PROGRESS" ] && break
  sleep 10
done
if [ "$primary_td" != "$new_td" ] || [ "$rollout" != "COMPLETED" ]; then
  log "Deploy did not stay on the new revision (primary: $primary_td, $rollout). The circuit breaker rolled back."
  exit 1
fi

# --- 5. Smoke test via the public URL ---
smoke_ok=false
for _ in $(seq 1 10); do
  version=$(curl -fsS --max-time 10 "$API_URL/api/health/" | jq -r '.version' 2>/dev/null || true)
  if [ "$version" = "$IMAGE_TAG" ]; then
    smoke_ok=true
    break
  fi
  log "health still reports version '${version:-?}'; retrying in 6 s"
  sleep 6
done
if $smoke_ok && curl -fsS --max-time 10 "$API_URL/" | grep -qi "<html"; then
  log "Smoke OK: version $IMAGE_TAG live at $API_URL"
else
  log "Smoke falhou."
  rollback
fi

# --- 6. Turn off again if the deploy was outside business hours ---
if $was_off && [ "$OFFICE_HOURS_ENABLED" = "true" ] && ! inside_office_hours; then
  log "Outside business hours: turning off again (ECS at 0, RDS stopping)."
  aws application-autoscaling register-scalable-target --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$RESOURCE_ID" \
    --min-capacity 0 --max-capacity 0
  aws rds stop-db-instance --db-instance-identifier "$DB_ID" >/dev/null || true
fi
log "Deploy done."

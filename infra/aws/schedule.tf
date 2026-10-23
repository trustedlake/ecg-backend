# Business hours (Mon-Fri, schedule timezone below): RDS 07:40-20:10, ECS 08:00-20:00.
# Outside that there is no task in Cloud Map and API Gateway answers 503 (the app shows the business-hours notice).
# Exceptions: scripts/start.sh and scripts/stop.sh.

resource "aws_appautoscaling_target" "backend" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"

  # Starts at 0 (the image only exists after the first build): ci/deploy.sh or the 08:00 action raise it
  # to min 1. After that the scheduled actions (and the scripts) change min/max during the day.
  min_capacity = 0
  max_capacity = 2

  lifecycle {
    ignore_changes = [min_capacity, max_capacity]
  }
}

resource "aws_appautoscaling_policy" "backend_cpu" {
  name               = "ecg-backend-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.backend.service_namespace
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 70
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_scheduled_action" "ecs_start" {
  count = var.office_hours_enabled ? 1 : 0

  name               = "ecg-backend-start"
  service_namespace  = aws_appautoscaling_target.backend.service_namespace
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension
  schedule           = var.ecs_start_cron
  timezone           = var.schedule_timezone

  scalable_target_action {
    min_capacity = 1
    max_capacity = 2
  }
}

resource "aws_appautoscaling_scheduled_action" "ecs_stop" {
  count = var.office_hours_enabled ? 1 : 0

  name               = "ecg-backend-stop"
  service_namespace  = aws_appautoscaling_target.backend.service_namespace
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension
  schedule           = var.ecs_stop_cron
  timezone           = var.schedule_timezone

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }

  # The Application Auto Scaling API does not accept two parallel scheduled actions on the same target.
  depends_on = [aws_appautoscaling_scheduled_action.ecs_start]
}

# --- RDS: EventBridge Scheduler with the SDK universal target ---

resource "aws_scheduler_schedule_group" "this" {
  count = var.office_hours_enabled ? 1 : 0
  name  = local.name
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler_rds" {
  count              = var.office_hours_enabled ? 1 : 0
  name               = "ecg-scheduler-rds-${var.region}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_rds" {
  statement {
    actions   = ["rds:StartDBInstance", "rds:StopDBInstance"]
    resources = [aws_db_instance.this.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_rds" {
  count  = var.office_hours_enabled ? 1 : 0
  name   = "start-stop-rds"
  role   = aws_iam_role.scheduler_rds[0].id
  policy = data.aws_iam_policy_document.scheduler_rds.json
}

locals {
  rds_schedules = var.office_hours_enabled ? {
    start = { action = "startDBInstance", cron = var.rds_start_cron }
    stop  = { action = "stopDBInstance", cron = var.rds_stop_cron }
  } : {}
}

resource "aws_scheduler_schedule" "rds" {
  for_each = local.rds_schedules

  name                         = "ecg-rds-${each.key}"
  group_name                   = aws_scheduler_schedule_group.this[0].name
  schedule_expression          = each.value.cron
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:${each.value.action}"
    role_arn = aws_iam_role.scheduler_rds[0].arn
    input    = jsonencode({ DbInstanceIdentifier = aws_db_instance.this.identifier })

    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 900
    }
  }
}

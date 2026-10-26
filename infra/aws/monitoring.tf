# 10 alarmes (cota gratuita do CloudWatch) → SNS ecg-alerts (e-mail) e budget mensal.
# notBreaching: when manually stopped there is no metric (ECS at 0, RDS stopped) and that is not an alarm.

resource "aws_sns_topic" "alerts" {
  name = "ecg-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarms = {
    "ecg-api-5xx" = {
      description = "API Gateway: 5xx responses (when manually stopped the 503 does not reach here: no app traffic)"
      namespace   = "AWS/ApiGateway"
      metric      = "5xx"
      dimensions  = { ApiId = aws_apigatewayv2_api.this.id, Stage = aws_apigatewayv2_stage.default.name }
      statistic   = "Sum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 5
    }
    "ecg-ecs-cpu" = {
      description = "ECS: average service CPU above 80% for 10 min"
      namespace   = "AWS/ECS"
      metric      = "CPUUtilization"
      dimensions  = { ClusterName = aws_ecs_cluster.this.name, ServiceName = aws_ecs_service.backend.name }
      statistic   = "Average"
      period      = 300
      periods     = 2
      comparison  = "GreaterThanThreshold"
      threshold   = 80
    }
    "ecg-ecs-memory" = {
      description = "ECS: average service memory above 85% for 10 min"
      namespace   = "AWS/ECS"
      metric      = "MemoryUtilization"
      dimensions  = { ClusterName = aws_ecs_cluster.this.name, ServiceName = aws_ecs_service.backend.name }
      statistic   = "Average"
      period      = 300
      periods     = 2
      comparison  = "GreaterThanThreshold"
      threshold   = 85
    }
    "ecg-rds-cpu" = {
      description = "RDS: CPU acima de 80% por 10 min"
      namespace   = "AWS/RDS"
      metric      = "CPUUtilization"
      dimensions  = { DBInstanceIdentifier = aws_db_instance.this.identifier }
      statistic   = "Average"
      period      = 300
      periods     = 2
      comparison  = "GreaterThanThreshold"
      threshold   = 80
    }
    "ecg-rds-free-storage" = {
      description = "RDS: menos de 2 GB livres"
      namespace   = "AWS/RDS"
      metric      = "FreeStorageSpace"
      dimensions  = { DBInstanceIdentifier = aws_db_instance.this.identifier }
      statistic   = "Minimum"
      period      = 300
      periods     = 1
      comparison  = "LessThanThreshold"
      threshold   = 2 * 1024 * 1024 * 1024
    }
    "ecg-rds-cpu-credits" = {
      description = "RDS: CPU credit balance below 20"
      namespace   = "AWS/RDS"
      metric      = "CPUCreditBalance"
      dimensions  = { DBInstanceIdentifier = aws_db_instance.this.identifier }
      statistic   = "Minimum"
      period      = 300
      periods     = 1
      comparison  = "LessThanThreshold"
      threshold   = 20
    }
    "ecg-dlq-messages" = {
      description = "SQS: task in the DLQ (failed 3 times in the worker)"
      namespace   = "AWS/SQS"
      metric      = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.tasks_dlq.name }
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 1
    }
    "ecg-queue-age" = {
      description = "SQS: oldest queue message older than 5 min (worker stopped or slow)"
      namespace   = "AWS/SQS"
      metric      = "ApproximateAgeOfOldestMessage"
      dimensions  = { QueueName = aws_sqs_queue.tasks.name }
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanThreshold"
      # A real reading takes 50-190 s (up to 900 s); above 10 min the queue is stuck.
      threshold = 600
    }
    "ecg-lambda-reading-errors" = {
      description = "Reading lambda: execution errors"
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = var.reading_lambda_name }
      statistic   = "Sum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 1
    }
    "ecg-lambda-interpretation-errors" = {
      description = "Interpretation lambda: execution errors"
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = var.interpretation_lambda_name }
      statistic   = "Sum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanOrEqualToThreshold"
      threshold   = 1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarms

  alarm_name          = each.key
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = each.value.statistic
  period              = each.value.period
  evaluation_periods  = each.value.periods
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# The budget tag filter only works with the cost allocation tag active (it was Inactive).
resource "aws_ce_cost_allocation_tag" "project" {
  tag_key = "project"
  status  = "Active"
}

resource "aws_budgets_budget" "monthly" {
  name         = "ecg-prod-monthly-${var.region}"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$ecg"]
  }

  depends_on = [aws_ce_cost_allocation_tag.project]

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alarm_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }
}

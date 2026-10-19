# 10 alarmes (cota gratuita do CloudWatch) → SNS ecg-alerts (e-mail) e budget mensal.
# notBreaching: fora do horário não há métrica (ECS em 0, RDS parado) e isso não é alarme.

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
      description = "API Gateway: respostas 5xx (fora do horário o 503 não chega aqui: sem tráfego do app)"
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
      description = "ECS: CPU média do serviço acima de 80% por 10 min"
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
      description = "ECS: memória média do serviço acima de 85% por 10 min"
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
      description = "RDS: saldo de créditos de CPU abaixo de 20"
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
      description = "SQS: tarefa na DLQ (falhou 3 vezes no worker)"
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
      description = "SQS: mensagem mais antiga da fila com mais de 5 min (worker parado ou lento)"
      namespace   = "AWS/SQS"
      metric      = "ApproximateAgeOfOldestMessage"
      dimensions  = { QueueName = aws_sqs_queue.tasks.name }
      statistic   = "Maximum"
      period      = 300
      periods     = 1
      comparison  = "GreaterThanThreshold"
      # Uma leitura real leva 50–190 s (até 900 s); acima de 10 min a fila está travada.
      threshold = 600
    }
    "ecg-lambda-reading-errors" = {
      description = "Lambda de leitura: erros de execução"
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
      description = "Lambda de interpretação: erros de execução"
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

# O filtro do budget por tag só funciona com a tag de alocação de custo ativa (estava Inactive).
resource "aws_ce_cost_allocation_tag" "project" {
  tag_key = "project"
  status  = "Active"
}

resource "aws_budgets_budget" "monthly" {
  name         = "ecg-prod-monthly"
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

# Fila das fases (leitura/interpretação) consumida pelo container worker (exams/management/commands/ecg_worker.py).

resource "aws_sqs_queue" "tasks_dlq" {
  name                      = "ecg-tasks-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

# Visibility maior que o read_timeout do boto (AWS_LAMBDA_READ_TIMEOUT=930 s; a ecg-reading roda
# até 900 s): a mensagem só reaparece se o worker morrer no meio da fase.
resource "aws_sqs_queue" "tasks" {
  name                       = "ecg-tasks"
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 960
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tasks_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "tasks_dlq" {
  queue_url = aws_sqs_queue.tasks_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.tasks.arn]
  })
}

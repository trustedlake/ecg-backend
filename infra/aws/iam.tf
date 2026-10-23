# ECS task roles (used in I2). Least privilege.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# --- Execution role: puxa a imagem, escreve logs e injeta os segredos do SSM ---

resource "aws_iam_role" "ecs_execution" {
  name               = "ecg-ecs-execution-${var.region}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid       = "ReadProdParameters"
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.ssm_prefix}/*"]
  }

  statement {
    sid       = "DecryptSecureString"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "ssm-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# --- Task role: what Django (web and worker) calls on AWS ---

resource "aws_iam_role" "ecs_task" {
  name               = "ecg-ecs-task-${var.region}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "ecs_task" {
  statement {
    sid       = "SignPhotoUpload"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${var.images_bucket}/*/original.*"]
  }

  statement {
    sid     = "ReadPhotosAndArtifacts"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${var.images_bucket}/*",
      "arn:aws:s3:::${var.results_bucket}/*",
    ]
  }

  statement {
    sid     = "InvokeContractLambdas"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.reading_lambda_name}",
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.interpretation_lambda_name}",
    ]
  }

  statement {
    sid = "TaskQueue"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.tasks.arn]
  }
}

resource "aws_iam_role_policy" "ecs_task" {
  name   = "ecg-backend"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task.json
}

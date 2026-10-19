# CI/CD na AWS: CodePipeline V2 (QUEUED) com duas fontes GitHub → CodeBuild ARM test → build → deploy.
# Buildspecs e deploy em ci/. A conexão do GitHub nasce PENDENTE: autorizar no console (ver README).

variable "github_owner" {
  type    = string
  default = "tl-gmiler"
}

variable "backend_repo" {
  type    = string
  default = "ecg-backend"
}

variable "frontend_repo" {
  type    = string
  default = "leitor-ecg"
}

variable "pipeline_branch" {
  type    = string
  default = "main"
}

resource "aws_codestarconnections_connection" "github" {
  name          = "ecg-github"
  provider_type = "GitHub"
}

# --- Bucket de artefatos ---

resource "aws_s3_bucket" "pipeline" {
  bucket = "ecg-backend-pipeline-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    id     = "expire-artifacts"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/codebuild/ecg-backend"
  retention_in_days = var.log_retention_days
}

# --- Roles do CodeBuild (uma por etapa) ---

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

locals {
  codebuild_stages = toset(["test", "build", "deploy"])

  # Comum às 3: logs no grupo próprio e leitura dos artefatos (build e deploy também gravam).
  codebuild_base_statements = [
    {
      sid       = "Logs"
      actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      resources = ["${aws_cloudwatch_log_group.codebuild.arn}:*"]
    },
    {
      sid       = "PipelineArtifacts"
      actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
      resources = ["${aws_s3_bucket.pipeline.arn}/*"]
    },
  ]

  ecs_service_arn = "arn:aws:ecs:${var.region}:${local.account_id}:service/${aws_ecs_cluster.this.name}/${aws_ecs_service.backend.name}"

  codebuild_stage_statements = {
    test = []
    build = [
      {
        sid       = "EcrLogin"
        actions   = ["ecr:GetAuthorizationToken"]
        resources = ["*"]
      },
      {
        sid = "EcrPush"
        actions = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        resources = [aws_ecr_repository.backend.arn]
      },
    ]
    deploy = [
      {
        sid       = "Service"
        actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
        resources = [local.ecs_service_arn]
      },
      {
        # Describe/Register de task definition não aceitam ARN de recurso.
        sid       = "TaskDefinitions"
        actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
        resources = ["*"]
      },
      {
        sid       = "OneOffTasks"
        actions   = ["ecs:RunTask"]
        resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${aws_ecs_task_definition.backend.family}:*"]
      },
      {
        sid       = "DescribeTasks"
        actions   = ["ecs:DescribeTasks"]
        resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.this.name}/*"]
      },
      {
        sid       = "PassTaskRoles"
        actions   = ["iam:PassRole"]
        resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn]
        condition = { test = "StringEquals", variable = "iam:PassedToService", values = ["ecs-tasks.amazonaws.com"] }
      },
      {
        sid       = "Database"
        actions   = ["rds:DescribeDBInstances", "rds:StartDBInstance", "rds:StopDBInstance"]
        resources = [aws_db_instance.this.arn]
      },
      {
        # Application Auto Scaling não tem permissão por recurso para estas ações.
        sid       = "Autoscaling"
        actions   = ["application-autoscaling:DescribeScalableTargets", "application-autoscaling:RegisterScalableTarget"]
        resources = ["*"]
      },
      {
        sid       = "OneOffLogs"
        actions   = ["logs:GetLogEvents"]
        resources = ["${aws_cloudwatch_log_group.backend.arn}:*"]
      },
    ]
  }
}

resource "aws_iam_role" "codebuild" {
  for_each           = local.codebuild_stages
  name               = "ecg-codebuild-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "codebuild" {
  for_each = local.codebuild_stages

  dynamic "statement" {
    for_each = concat(local.codebuild_base_statements, local.codebuild_stage_statements[each.key])
    content {
      sid       = statement.value.sid
      actions   = statement.value.actions
      resources = statement.value.resources

      dynamic "condition" {
        for_each = can(statement.value.condition) ? [statement.value.condition] : []
        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

resource "aws_iam_role_policy" "codebuild" {
  for_each = local.codebuild_stages
  name     = "ecg-codebuild-${each.key}"
  role     = aws_iam_role.codebuild[each.key].id
  policy   = data.aws_iam_policy_document.codebuild[each.key].json
}

# --- Projetos CodeBuild ---

locals {
  codebuild_env = {
    test  = {}
    build = { ECR_REPOSITORY_URL = aws_ecr_repository.backend.repository_url }
    deploy = {
      ECR_REPOSITORY_URL   = aws_ecr_repository.backend.repository_url
      API_URL              = aws_apigatewayv2_stage.default.invoke_url
      CLUSTER              = aws_ecs_cluster.this.name
      SERVICE              = aws_ecs_service.backend.name
      TASK_FAMILY          = aws_ecs_task_definition.backend.family
      DB_ID                = aws_db_instance.this.identifier
      LOG_GROUP            = aws_cloudwatch_log_group.backend.name
      OFFICE_HOURS_ENABLED = tostring(var.office_hours_enabled)
    }
  }
}

resource "aws_codebuild_project" "stage" {
  for_each = local.codebuild_stages

  name          = "ecg-backend-${each.key}"
  service_role  = aws_iam_role.codebuild[each.key].arn
  build_timeout = 20

  source {
    type      = "CODEPIPELINE"
    buildspec = "ci/buildspec-${each.key}.yml"
  }

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    type            = "ARM_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    privileged_mode = each.key == "build"

    dynamic "environment_variable" {
      for_each = local.codebuild_env[each.key]
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.codebuild.name
      stream_name = each.key
    }
  }
}

# --- Pipeline ---

data "aws_iam_policy_document" "codepipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "codepipeline" {
  name               = "ecg-codepipeline"
  assume_role_policy = data.aws_iam_policy_document.codepipeline_assume.json
}

data "aws_iam_policy_document" "codepipeline" {
  statement {
    sid       = "Artifacts"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketVersioning"]
    resources = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"]
  }

  statement {
    sid       = "GitHub"
    actions   = ["codestar-connections:UseConnection", "codeconnections:UseConnection"]
    resources = [aws_codestarconnections_connection.github.arn]
  }

  statement {
    sid       = "Builds"
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
    resources = [for p in aws_codebuild_project.stage : p.arn]
  }
}

resource "aws_iam_role_policy" "codepipeline" {
  name   = "ecg-codepipeline"
  role   = aws_iam_role.codepipeline.id
  policy = data.aws_iam_policy_document.codepipeline.json
}

resource "aws_codepipeline" "this" {
  name           = "ecg-backend"
  role_arn       = aws_iam_role.codepipeline.arn
  pipeline_type  = "V2"
  execution_mode = "QUEUED"

  artifact_store {
    type     = "S3"
    location = aws_s3_bucket.pipeline.bucket
  }

  stage {
    name = "Source"

    action {
      name             = "Backend"
      namespace        = "SourceBackend"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["backend"]
      configuration = {
        ConnectionArn    = aws_codestarconnections_connection.github.arn
        FullRepositoryId = "${var.github_owner}/${var.backend_repo}"
        BranchName       = var.pipeline_branch
      }
    }

    action {
      name             = "Frontend"
      namespace        = "SourceFrontend"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["frontend"]
      configuration = {
        ConnectionArn    = aws_codestarconnections_connection.github.arn
        FullRepositoryId = "${var.github_owner}/${var.frontend_repo}"
        BranchName       = var.pipeline_branch
      }
    }
  }

  stage {
    name = "Test"
    action {
      name            = "Test"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["backend"]
      configuration   = { ProjectName = aws_codebuild_project.stage["test"].name }
    }
  }

  stage {
    name = "Build"
    action {
      name             = "Build"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["backend", "frontend"]
      output_artifacts = ["image"]
      configuration = {
        ProjectName   = aws_codebuild_project.stage["build"].name
        PrimarySource = "backend"
        EnvironmentVariables = jsonencode([
          { name = "BACKEND_SHA", value = "#{SourceBackend.CommitId}", type = "PLAINTEXT" },
          { name = "FRONTEND_SHA", value = "#{SourceFrontend.CommitId}", type = "PLAINTEXT" },
        ])
      }
    }
  }

  stage {
    name = "Deploy"
    action {
      name            = "Deploy"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["backend", "image"]
      configuration = {
        ProjectName   = aws_codebuild_project.stage["deploy"].name
        PrimarySource = "backend"
      }
    }
  }

  # Push na main de qualquer um dos dois repos dispara o pipeline.
  trigger {
    provider_type = "CodeStarSourceConnection"
    git_configuration {
      source_action_name = "Backend"
      push {
        branches {
          includes = [var.pipeline_branch]
        }
      }
    }
  }

  trigger {
    provider_type = "CodeStarSourceConnection"
    git_configuration {
      source_action_name = "Frontend"
      push {
        branches {
          includes = [var.pipeline_branch]
        }
      }
    }
  }
}

# --- Aviso de falha no SNS ecg-alerts ---

resource "aws_cloudwatch_event_rule" "pipeline_failed" {
  name        = "ecg-pipeline-failed"
  description = "Execução do pipeline ecg-backend falhou"
  event_pattern = jsonencode({
    source      = ["aws.codepipeline"]
    detail-type = ["CodePipeline Pipeline Execution State Change"]
    detail = {
      pipeline = [aws_codepipeline.this.name]
      state    = ["FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "pipeline_failed" {
  rule = aws_cloudwatch_event_rule.pipeline_failed.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      pipeline  = "$.detail.pipeline"
      execution = "$.detail.execution-id"
    }
    input_template = "\"Pipeline <pipeline> falhou (execução <execution>). Veja o CodePipeline no console, região ${var.region}.\""
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AccountPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "EventBridgePublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.pipeline_failed.arn]
    }
  }

  statement {
    sid       = "CloudWatchAlarms"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

output "pipeline_name" {
  value = aws_codepipeline.this.name
}

output "github_connection_arn" {
  description = "Conexão PENDENTE até ser autorizada no console (Developer Tools > Connections)."
  value       = aws_codestarconnections_connection.github.arn
}

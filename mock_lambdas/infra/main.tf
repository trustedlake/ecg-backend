# Lambdas MOCK de leitura e interpretação de ECG (contrato v1, CONTRATO_LAMBDAS.md).
# Stack independente: state próprio, só referencia os buckets já existentes do
# ecg-lambdas (sem alterá-los). Rode ../build.sh antes do plan/apply.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  backend "s3" {
    bucket = "ecg-lambdas-terraform-state"
    key    = "ecg-mock-lambdas/terraform.tfstate"
    region = "sa-east-1"
  }
}

provider "aws" {
  region  = "sa-east-1"
  profile = var.aws_profile

  default_tags {
    tags = {
      project = "ecg"
      stack   = "ecg-mock-lambdas"
    }
  }
}

data "aws_s3_bucket" "images" {
  bucket = var.images_bucket
}

data "aws_s3_bucket" "results" {
  bucket = var.results_bucket
}

locals {
  mock_env = var.mock_scenario == "" ? {} : { MOCK_SCENARIO = var.mock_scenario }
  functions = {
    reading = {
      name        = "ecg-reading-mock"
      memory_size = 1769 # 1 vCPU inteira: o PNG do overlay é CPU-bound
      timeout     = 60
      environment = merge({ RESULTS_BUCKET = data.aws_s3_bucket.results.id, CODE_VERSION = var.code_version }, local.mock_env)
    }
    interpretation = {
      name        = "ecg-interpretation-mock"
      memory_size = 256
      timeout     = 30
      environment = merge({ CODE_VERSION = var.code_version }, local.mock_env)
    }
  }
}

data "archive_file" "package" {
  for_each    = local.functions
  type        = "zip"
  source_dir  = "${path.module}/../build/${each.key}"
  output_path = "${path.module}/../build/${each.key}.zip"
}

resource "aws_iam_role" "this" {
  for_each = local.functions
  name     = "${each.value.name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic" {
  for_each   = local.functions
  role       = aws_iam_role.this[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Leitura: lê a foto no bucket de imagens e grava artefatos só sob reading/ no de resultados.
resource "aws_iam_role_policy" "reading_s3" {
  name = "ecg-reading-mock-s3"
  role = aws_iam_role.this["reading"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${data.aws_s3_bucket.images.arn}/*"
      },
      {
        # Sem ListBucket, objeto inexistente volta 403; a lambda trata como IMAGE_NOT_FOUND.
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${data.aws_s3_bucket.results.arn}/reading/*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.functions
  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = 30
}

resource "aws_lambda_function" "this" {
  for_each         = local.functions
  function_name    = each.value.name
  description      = "MOCK ${each.key} de ECG — contrato v1 (ecg-backend/CONTRATO_LAMBDAS.md)"
  role             = aws_iam_role.this[each.key].arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = each.value.timeout
  memory_size      = each.value.memory_size
  filename         = data.archive_file.package[each.key].output_path
  source_code_hash = data.archive_file.package[each.key].output_base64sha256

  environment {
    variables = each.value.environment
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy_attachment.basic]
}

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Reusa o bucket de state do ecg-lambdas, em key propria - nao toca o state dele.
  backend "s3" {
    bucket = "ecg-lambdas-terraform-state"
    key    = "ecg-backend/terraform.tfstate"
    region = "sa-east-1"
  }
}

provider "aws" {
  region  = "sa-east-1"
  profile = var.aws_profile
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# AMI: Amazon Linux 2023 arm64 (Graviton) - nunca hardcodar ID de AMI.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# VPC e subnet default (decisao: manter simples, sem rede nova).
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_subnet" "chosen" {
  id = data.aws_subnets.default.ids[0]
}

locals {
  app_name       = "ecg-backend"
  app_dir        = "/home/ecgapp/ecg-backend"
  ssm_db_url     = "/ecg-backend/DATABASE_URL"
  ssm_secret_key = "/ecg-backend/DJANGO_SECRET_KEY"
  lambda_prefix  = "ecg-lambdas-lambda-"
  images_bucket  = "ecg-lambdas-images-785236377332-sa-east-1"
  results_bucket = "ecg-lambdas-results-785236377332-sa-east-1"
  lambda_arns = [
    "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.lambda_prefix}1-gridline-and-trace",
    "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.lambda_prefix}2-signal-extraction",
    "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.lambda_prefix}3-diagnosis",
  ]
}

# ---------- Key pair ----------
# A chave privada NUNCA passa pelo Terraform (isso gravaria em texto claro no
# tfstate). O par e gerado localmente antes do apply:
#   ssh-keygen -t ed25519 -f infra/keys/ecg-backend -N ""
# e o aws_key_pair so registra a chave PUBLICA existente em infra/key.
# Se var.key_name vier preenchido, reaproveita um key pair AWS existente.
resource "aws_key_pair" "ecg_backend" {
  count      = var.key_name == "" ? 1 : 0
  key_name   = "${local.app_name}-deploy"
  public_key = file("${path.module}/../keys/ecg-backend.pub")
}

# ---------- Security Group ----------
resource "aws_security_group" "ecg_backend" {
  name        = "${local.app_name}-sg"
  description = "API publica (8000) + SSH restrito (22) para ${local.app_name}"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "API HTTP - Gunicorn (sem nginx/ALB)"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH - only from CIDR set at apply (no default)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_allowed_cidr]
  }

  egress {
    description = "Outbound internet (Neon, S3, lambdas)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- IAM (role EC2, sem access keys) ----------
resource "aws_iam_role" "ecg_backend" {
  name = "${local.app_name}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_instance_profile" "ecg_backend" {
  name = "${local.app_name}-ec2-profile"
  role = aws_iam_role.ecg_backend.name
}

resource "aws_iam_role_policy" "ecg_backend" {
  name = "${local.app_name}-ec2-policy"
  role = aws_iam_role.ecg_backend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.lambda_arns
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = ["arn:aws:s3:::${local.images_bucket}", "arn:aws:s3:::${local.images_bucket}/*"]
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["arn:aws:s3:::${local.results_bucket}", "arn:aws:s3:::${local.results_bucket}/*"]
      },
      {
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_db_url}",
          "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_secret_key}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      },
    ]
  })
}

# ---------- SSM Parameter Store (SecureString, valor placeholder) ----------
resource "aws_ssm_parameter" "database_url" {
  name = local.ssm_db_url
  type = "SecureString"
  # Placeholder - preenchido a mao via aws ssm put-parameter --overwrite
  # (nunca vai pro terraform nem pro git).
  value = "CHANGE_ME_DATABASE_URL"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "secret_key" {
  name  = local.ssm_secret_key
  type  = "SecureString"
  value = "CHANGE_ME_DJANGO_SECRET_KEY"
  lifecycle {
    ignore_changes = [value]
  }
}

# ---------- EC2 t4g.nano (Graviton, AL2023) ----------
resource "aws_instance" "ecg_backend" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnet.chosen.id
  vpc_security_group_ids      = [aws_security_group.ecg_backend.id]
  iam_instance_profile        = aws_iam_instance_profile.ecg_backend.name
  key_name                    = var.key_name != "" ? var.key_name : aws_key_pair.ecg_backend[0].key_name
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    app_dir        = local.app_dir
    ssm_db_url     = local.ssm_db_url
    ssm_secret_key = local.ssm_secret_key
    aws_region     = data.aws_region.current.name
  })

  tags = {
    Name = local.app_name
  }
}
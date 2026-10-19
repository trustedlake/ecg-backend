# Infra de produção do backend ECG (docs/plano-migracao-v1.md §0, etapas I1–I3).
# Esta stack NÃO gerencia os buckets nem as lambdas: eles vêm dos stacks ecg-lambdas e mock_lambdas.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x: password_wo em aws_db_instance e ephemeral aws_ssm_parameter (testado com 6.69.1).
      version = "~> 6.69"
    }
  }

  backend "s3" {
    bucket = "ecg-lambdas-terraform-state"
    key    = "ecg-backend-prod/terraform.tfstate"
    region = "sa-east-1"
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      project = "ecg"
      env     = "prod"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name       = "ecg"
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
}

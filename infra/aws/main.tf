# Production infra for the ECG backend (docs/migration-plan-v1.md §0, steps I1-I3).
# This stack does NOT manage the buckets or the lambdas: they come from the trustedlake/ecg-lambdas-v1 stacks (ecg-reading, ecg-interpretation).

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x: password_wo on aws_db_instance and ephemeral aws_ssm_parameter (tested with 6.69.1).
      version = "~> 6.69"
    }
  }

  backend "s3" {
    bucket = "ecg-terraform-state-785236377332-us-east-1"
    key    = "ecg-backend-prod/terraform.tfstate"
    region = "us-east-1"
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

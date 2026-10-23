variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  type        = string
  description = "Local AWS profile (~/.aws/credentials). In CodeBuild, leave empty to use the role."
  default     = "trustedlake"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "images_bucket" {
  type        = string
  description = "Bucket (existente, stack ecg-lambdas) onde o app sobe as fotos."
  default     = "ecg-lambdas-images-785236377332-us-east-1"
}

variable "results_bucket" {
  type        = string
  description = "Bucket (existing, ecg-lambdas stack) with overlay, refusal preview and signal."
  default     = "ecg-lambdas-results-785236377332-us-east-1"
}

variable "reading_lambda_name" {
  type        = string
  description = "Reading lambda (contract 2.0, ecg-lambdas-v1). Test scenarios go in the payload mock field."
  default     = "ecg-reading"
}

variable "interpretation_lambda_name" {
  type    = string
  default = "ecg-interpretation"
}

variable "ssm_prefix" {
  type        = string
  description = "Prefix of the SecureString parameters created by hand (see README)."
  default     = "/ecg/prod"
}

variable "db_engine_version" {
  type    = string
  default = "17.11"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_password_version" {
  type        = number
  description = "Increment to resend the SSM password to RDS (password_wo is only applied when it changes)."
  default     = 1
}

# --- I2: ECS, API Gateway, schedule and monitoring ---

variable "image_tag" {
  type        = string
  description = "Image tag in ECR. The pipeline (I3) registers new revisions; Terraform only creates the first one."
  default     = "bootstrap"
}

variable "task_cpu" {
  type        = number
  description = "Task CPU (512 = 0.5 vCPU). Raise to 1024 if the load test calls for it."
  default     = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "test_scenarios_enabled" {
  type        = bool
  description = "Enables test_scenario (mock) for is_staff users."
  default     = true
}

variable "extra_allowed_hosts" {
  type        = list(string)
  description = "Hosts extras no ALLOWED_HOSTS (ex.: se o VPC Link entregar outro Host)."
  default     = []
}

variable "office_hours_enabled" {
  type        = bool
  description = "Turns ECS and RDS on/off on business hours. false = always on (minimum 1 task)."
  default     = true
}

variable "schedule_timezone" {
  type    = string
  default = "America/Sao_Paulo"
}

variable "ecs_start_cron" {
  type    = string
  default = "cron(0 8 ? * MON-FRI *)"
}

variable "ecs_stop_cron" {
  type    = string
  default = "cron(0 20 ? * MON-FRI *)"
}

variable "rds_start_cron" {
  type    = string
  default = "cron(40 7 ? * MON-FRI *)"
}

variable "rds_stop_cron" {
  type    = string
  default = "cron(10 20 ? * MON-FRI *)"
}

variable "alarm_email" {
  type        = string
  description = "Email that receives alarms and cost notices (confirm the SNS subscription)."
  default     = "gustavo.miler@trustedlake.com"
}

variable "monthly_budget_usd" {
  type    = number
  default = 35
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "region" {
  type    = string
  default = "sa-east-1"
}

variable "aws_profile" {
  type        = string
  description = "Profile AWS local (~/.aws/credentials). No CodeBuild, deixe vazio para usar a role."
  default     = "trustedlake"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "images_bucket" {
  type        = string
  description = "Bucket (existente, stack ecg-lambdas) onde o app sobe as fotos."
  default     = "ecg-lambdas-images-785236377332-sa-east-1"
}

variable "results_bucket" {
  type        = string
  description = "Bucket (existente, stack ecg-lambdas) com overlay, prévia de recusa e sinal."
  default     = "ecg-lambdas-results-785236377332-sa-east-1"
}

variable "reading_lambda_name" {
  type        = string
  description = "Lambda de leitura do contrato v1 (trocar mock por real = trocar aqui e na task)."
  default     = "ecg-reading-mock"
}

variable "interpretation_lambda_name" {
  type    = string
  default = "ecg-interpretation-mock"
}

variable "ssm_prefix" {
  type        = string
  description = "Prefixo dos parâmetros SecureString criados à mão (ver README)."
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
  description = "Incremente para reenviar a senha do SSM ao RDS (password_wo só é aplicado quando muda)."
  default     = 1
}

# --- I2: ECS, API Gateway, horário e monitoramento ---

variable "image_tag" {
  type        = string
  description = "Tag da imagem no ECR. O pipeline (I3) registra revisões novas; o Terraform só cria a primeira."
  default     = "bootstrap"
}

variable "task_cpu" {
  type        = number
  description = "CPU da task (512 = 0,5 vCPU). Subir para 1024 se o teste de carga pedir."
  default     = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "test_scenarios_enabled" {
  type        = bool
  description = "Libera test_scenario (mock) para usuários is_staff."
  default     = true
}

variable "extra_allowed_hosts" {
  type        = list(string)
  description = "Hosts extras no ALLOWED_HOSTS (ex.: se o VPC Link entregar outro Host)."
  default     = []
}

variable "office_hours_enabled" {
  type        = bool
  description = "Liga/desliga ECS e RDS no horário comercial. false = sempre ligado (mínimo 1 task)."
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
  description = "E-mail que recebe alarmes e avisos de custo (confirmar a assinatura do SNS)."
}

variable "monthly_budget_usd" {
  type    = number
  default = 35
}

variable "log_retention_days" {
  type    = number
  default = 14
}

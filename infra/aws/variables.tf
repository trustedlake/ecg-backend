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

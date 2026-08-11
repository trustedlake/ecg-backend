variable "aws_profile" {
  type        = string
  description = "Perfil AWS local (~/.aws/credentials) usado pelo provider."
  default     = "trustedlake"
}

variable "ssh_allowed_cidr" {
  type        = string
  description = "CIDR liberado no security group para SSH (porta 22). SEM default: obrigatório informar o IP de casa no apply. Ex.: 203.0.113.42/32"
}

variable "instance_type" {
  type        = string
  description = "Tipo da instância EC2 (arm64/Graviton)."
  default     = "t4g.nano"
}

variable "key_name" {
  type        = string
  description = "Se preenchido, reusa um key pair AWS existente em vez de gerar um novo. Deixe vazio para gerar (default)."
  default     = ""
}
variable "aws_profile" {
  type        = string
  description = "Local AWS profile (~/.aws/credentials) used by the provider."
  default     = "trustedlake"
}

variable "ssh_allowed_cidr" {
  type        = string
  description = "CIDR allowed in the security group for SSH (port 22). NO default: the home IP must be given at apply. E.g. 203.0.113.42/32"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type (arm64/Graviton)."
  default     = "t4g.nano"
}

variable "key_name" {
  type        = string
  description = "If set, reuses an existing AWS key pair instead of generating a new one. Leave empty to generate (default)."
  default     = ""
}
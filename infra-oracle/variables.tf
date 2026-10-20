variable "vm_host" {
  description = "Public IP of the shared Oracle VM."
  type        = string
  default     = "147.15.66.165"
}

variable "vm_ssh_user" {
  description = "VM OS user (Ubuntu 24.04)."
  type        = string
  default     = "ubuntu"
}

variable "vm_ssh_private_key_path" {
  description = "Local path of the SSH private key with access to the VM."
  type        = string
}

variable "image_tag" {
  description = "Tag of the ghcr.io/tl-gmiler/ecg-backend image to run."
  type        = string
  default     = "latest"
}

variable "ghcr_username" {
  description = "User for the ghcr.io login (only needed if the package is private)."
  type        = string
  default     = ""
}

variable "ghcr_token" {
  description = "Token (PAT with read:packages scope) for the ghcr.io login."
  type        = string
  default     = ""
  sensitive   = true
}

variable "app_port" {
  description = "Host port mapped to the app (the container listens on 8000). 8002/8010/8011/8081/8012/8082/8013 are already used on the same VM; 8014 is the next free one."
  type        = number
  default     = 8014
}

variable "app_env" {
  description = "Container environment variables (equivalent to the current .env on the EC2). Fill in via a local terraform.tfvars (not committed) or TF_VAR_app_env."
  type        = map(string)
  default     = {}
  sensitive   = true
}

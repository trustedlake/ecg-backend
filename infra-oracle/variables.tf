variable "vm_host" {
  description = "IP publico da VM Oracle compartilhada."
  type        = string
  default     = "147.15.66.165"
}

variable "vm_ssh_user" {
  description = "Usuario SO da VM (Ubuntu 24.04)."
  type        = string
  default     = "ubuntu"
}

variable "vm_ssh_private_key_path" {
  description = "Caminho local da chave privada SSH com acesso a VM."
  type        = string
}

variable "image_tag" {
  description = "Tag da imagem ghcr.io/tl-gmiler/ecg-backend a subir."
  type        = string
  default     = "latest"
}

variable "ghcr_username" {
  description = "Usuario pra login no ghcr.io (necessario so se o pacote for privado)."
  type        = string
  default     = ""
}

variable "ghcr_token" {
  description = "Token (PAT com escopo read:packages) pra login no ghcr.io."
  type        = string
  default     = ""
  sensitive   = true
}

variable "app_port" {
  description = "Porta do host mapeada pro app (container escuta em 8000). 8002/8010/8011/8081/8012/8082/8013 ja usados na mesma VM - 8014 e a proxima livre."
  type        = number
  default     = 8014
}

variable "app_env" {
  description = "Variaveis de ambiente do container (equivalente ao .env de hoje na EC2). Preencher via terraform.tfvars local (nao commitado) ou TF_VAR_app_env."
  type        = map(string)
  default     = {}
  sensitive   = true
}

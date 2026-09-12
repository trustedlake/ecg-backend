# Infra do ecg-backend na VM Oracle compartilhada.
#
# NAO gerencia a VM em si (compartilhada com Lakeship + persona + content + lps).
# So gerencia o container Docker deste app, conectando no daemon Docker remoto
# via SSH.
#
# O CloudFront que ja expoe este app publicamente (d1i155fprtk4at.cloudfront.net,
# distribution E1919M81GCZ1D7) e um recurso PRE-EXISTENTE gerenciado pelo
# terraform ANTIGO em infra/terraform/ deste mesmo repo (ecg-backend) - que ja
# esta com state desatualizado/drift (a EC2 dedicada que ele criava foi
# terminada faz tempo; hoje o app roda manualmente na EC2 compartilhada). Este
# modulo NAO adota nem duplica esse CloudFront - o reapontamento do origin pra
# VM Oracle (depois que o container aqui estiver validado) e feito
# manualmente via AWS CLI, fora do terraform, pra nao arriscar conflito de
# state. Ver README.md.

terraform {
  required_version = ">= 1.5"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

provider "docker" {
  host = "ssh://${var.vm_ssh_user}@${var.vm_host}:22"

  ssh_opts = [
    "-i", var.vm_ssh_private_key_path,
    "-o", "StrictHostKeyChecking=accept-new",
  ]

  dynamic "registry_auth" {
    for_each = var.ghcr_username != "" ? [1] : []
    content {
      address  = "ghcr.io"
      username = var.ghcr_username
      password = var.ghcr_token
    }
  }
}

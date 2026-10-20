# ecg-backend infra on the shared Oracle VM.
#
# Does NOT manage the VM itself (shared with Lakeship + persona + content + lps).
# So gerencia o container Docker deste app, conectando no daemon Docker remoto
# via SSH.
#
# The CloudFront that already exposes this app publicly (d1i155fprtk4at.cloudfront.net,
# distribution E1919M81GCZ1D7) is a PRE-EXISTING resource managed by the
# OLD terraform in infra/terraform/ of this same repo (ecg-backend), which already
# has stale state/drift (the dedicated EC2 it created was
# terminated long ago; the app now runs manually on the shared host). This
# module does NOT adopt or duplicate that CloudFront: repointing the origin to the
# Oracle VM (once the container here is validated) is done
# manually via the AWS CLI, outside terraform, to avoid risking a conflict with
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

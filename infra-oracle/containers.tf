resource "docker_image" "app" {
  name         = "ghcr.io/tl-gmiler/ecg-backend:${var.image_tag}"
  keep_locally = true
}

resource "docker_container" "app" {
  name  = "ecg-backend"
  image = docker_image.app.image_id

  restart = "unless-stopped"

  ports {
    internal = 8000
    external = var.app_port
  }

  env = [for k, v in var.app_env : "${k}=${v}"]
}

output "container_name" {
  description = "Docker container name on the Oracle VM."
  value       = docker_container.app.name
}

output "app_port" {
  description = "VM host port mapped to the app (the container listens on 8000)."
  value       = var.app_port
}

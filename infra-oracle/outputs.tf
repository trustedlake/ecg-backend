output "container_name" {
  description = "Nome do container Docker na VM Oracle."
  value       = docker_container.app.name
}

output "app_port" {
  description = "Porta do host da VM mapeada pro app (container escuta em 8000)."
  value       = var.app_port
}

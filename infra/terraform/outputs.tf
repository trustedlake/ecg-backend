output "instance_id" {
  description = "ID da instância EC2 do ecg-backend."
  value       = aws_instance.ecg_backend.id
}

output "instance_public_ip" {
  description = "IP público da instância (endpoint da API e destino do deploy.sh)."
  value       = aws_instance.ecg_backend.public_ip
}

output "ssh_key_name" {
  description = "Key pair usado para SSH (gerado ou reutilizado)."
  value       = var.key_name != "" ? var.key_name : aws_key_pair.ecg_backend[0].key_name
}

output "private_key_path" {
  description = "Caminho do PEM local (vazio se reutilizou um key pair existente)."
  value       = var.key_name == "" ? "${path.module}/../keys/${local.app_name}.pem" : ""
}

output "elastic_ip" {
  description = "IP público fixo da instância (Elastic IP)."
  value       = aws_eip.ecg_backend.public_ip
}

output "cloudfront_domain" {
  description = "Domínio HTTPS do CloudFront na frente do backend."
  value       = aws_cloudfront_distribution.ecg_backend.domain_name
}
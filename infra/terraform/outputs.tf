output "instance_id" {
  description = "ID of the ecg-backend EC2 instance."
  value       = aws_instance.ecg_backend.id
}

output "instance_public_ip" {
  description = "Public IP of the instance (API endpoint and deploy.sh target)."
  value       = aws_instance.ecg_backend.public_ip
}

output "ssh_key_name" {
  description = "Key pair used for SSH (generated or reused)."
  value       = var.key_name != "" ? var.key_name : aws_key_pair.ecg_backend[0].key_name
}

output "private_key_path" {
  description = "Local PEM path (empty if an existing key pair was reused)."
  value       = var.key_name == "" ? "${path.module}/../keys/${local.app_name}.pem" : ""
}

output "elastic_ip" {
  description = "Fixed public IP of the instance (Elastic IP)."
  value       = aws_eip.ecg_backend.public_ip
}

output "cloudfront_domain" {
  description = "HTTPS domain of the CloudFront in front of the backend."
  value       = aws_cloudfront_distribution.ecg_backend.domain_name
}
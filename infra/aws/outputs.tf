output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "security_group_ids" {
  value = {
    vpclink = aws_security_group.vpclink.id
    ecs     = aws_security_group.ecs.id
    rds     = aws_security_group.rds.id
  }
}

output "db_endpoint" {
  description = "host:porta do RDS (o backend usa DB_HOST = só o host)."
  value       = aws_db_instance.this.endpoint
}

output "db_host" {
  value = aws_db_instance.this.address
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "db_username" {
  value = aws_db_instance.this.username
}

output "ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "queue_url" {
  value = aws_sqs_queue.tasks.url
}

output "queue_arn" {
  value = aws_sqs_queue.tasks.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.tasks_dlq.arn
}

output "ecs_execution_role_arn" {
  value = aws_iam_role.ecs_execution.arn
}

output "ecs_task_role_arn" {
  value = aws_iam_role.ecs_task.arn
}

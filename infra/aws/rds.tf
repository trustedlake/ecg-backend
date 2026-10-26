# Single-AZ PostgreSQL. The password comes from SSM through an ephemeral resource and goes into password_wo:
# it is never in the plan or the state. The database runs continuously.

ephemeral "aws_ssm_parameter" "db_password" {
  arn             = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.ssm_prefix}/DB_PASSWORD"
  with_decryption = true
}

resource "aws_db_subnet_group" "this" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.name}-postgres17"
  family = "postgres17"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot" # static parameter; applies at creation
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "this" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name                    = "ecg"
  username                   = "ecg_admin"
  password_wo                = ephemeral.aws_ssm_parameter.db_password.value
  password_wo_version        = var.db_password_version
  parameter_group_name       = aws_db_parameter_group.this.name
  auto_minor_version_upgrade = true

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  multi_az               = false
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  # Windows within the on-hours (Mon-Fri 10:40-23:10 UTC).
  backup_retention_period = 7
  backup_window           = "14:00-14:30"
  maintenance_window      = "wed:15:00-wed:15:30"
  copy_tags_to_snapshot   = true

  # Free tier (7 days).
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"

  tags = { Name = local.name }
}

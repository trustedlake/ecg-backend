# PostgreSQL Single-AZ. A senha vem do SSM por um recurso ephemeral e vai em password_wo:
# não fica no plan nem no state. Fora do horário comercial o banco é parado pelo Scheduler (I2).

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
    name  = "rds.force_ssl"
    value = "1"
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

  # Janelas dentro do horário ligado (seg–sex 07:40–20:10 BRT = 10:40–23:10 UTC).
  backup_retention_period = 7
  backup_window           = "14:00-14:30"
  maintenance_window      = "wed:15:00-wed:15:30"
  copy_tags_to_snapshot   = true

  # Nível gratuito (7 dias).
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"

  tags = { Name = local.name }
}

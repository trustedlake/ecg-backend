# ECS Fargate (ARM): uma task com dois containers da mesma imagem — `web` (gunicorn + SPA) e
# `worker` (manage.py ecg_worker, long polling na SQS). Entrada só pelo VPC Link via Cloud Map (api.tf).

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/ecg-backend"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

# --- Cloud Map: o HTTP API descobre IP e porta das tasks pelo registro SRV ---

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${local.name}.internal"
  vpc  = aws_vpc.this.id
}

resource "aws_service_discovery_service" "backend" {
  name = "backend"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "SRV"
      ttl  = 10
    }
  }

  # O ECS informa a saúde (health check do container web) e tira do Cloud Map a task doente.
  # O bloco vazio não chega à AWS (o provider omite); failure_threshold (obsoleto, a AWS sempre usa 1) o força.
  health_check_custom_config {
    failure_threshold = 1
  }
}

# --- Task definition ---

locals {
  api_host  = "${aws_apigatewayv2_api.this.id}.execute-api.${var.region}.amazonaws.com"
  image_uri = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"

  # Host do execute-api para o tráfego real; localhost para o health check do próprio container.
  allowed_hosts = concat([local.api_host, "localhost", "127.0.0.1"], var.extra_allowed_hosts)

  container_environment = [for k, v in {
    DEBUG                         = "False"
    AWS_REGION                    = var.region
    ALLOWED_HOSTS                 = join(",", local.allowed_hosts)
    CSRF_TRUSTED_ORIGINS          = "https://${local.api_host}"
    CORS_ALLOWED_ORIGINS          = "https://${local.api_host}"
    SECURE_HSTS_SECONDS           = "31536000"
    DB_HOST                       = aws_db_instance.this.address
    DB_NAME                       = aws_db_instance.this.db_name
    DB_USER                       = aws_db_instance.this.username
    AWS_IMAGES_BUCKET             = var.images_bucket
    AWS_RESULTS_BUCKET            = var.results_bucket
    AWS_LAMBDA_ECG_READING        = var.reading_lambda_name
    AWS_LAMBDA_ECG_INTERPRETATION = var.interpretation_lambda_name
    ECG_TASK_MODE                 = "sqs"
    ECG_TASK_QUEUE_URL            = aws_sqs_queue.tasks.url
    ECG_TEST_SCENARIOS_ENABLED    = tostring(var.test_scenarios_enabled)
    APP_VERSION                   = var.image_tag
  } : { name = k, value = v }]

  container_secrets = [for k in ["DB_PASSWORD", "DJANGO_SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD"] : {
    name      = k
    valueFrom = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.ssm_prefix}/${k}"
  }]

  log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.backend.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "ecg"
    }
  }
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "ecg-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name         = "web"
      image        = local.image_uri
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment  = local.container_environment
      secrets      = local.container_secrets
      healthCheck = {
        command = [
          "CMD", "python", "-c",
          "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health/', timeout=3)",
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
      logConfiguration = local.log_configuration
    },
    {
      name             = "worker"
      image            = local.image_uri
      essential        = true
      command          = ["python", "manage.py", "ecg_worker"]
      stopTimeout      = 120
      environment      = local.container_environment
      secrets          = local.container_secrets
      logConfiguration = local.log_configuration
    },
  ])
}

# --- Serviço ---

resource "aws_ecs_service" "backend" {
  name             = "ecg-backend"
  cluster          = aws_ecs_cluster.this.id
  task_definition  = aws_ecs_task_definition.backend.arn
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  # Começa em 0: a imagem só existe depois do primeiro build do pipeline (I3). A partir daí quem
  # manda na quantidade é o autoscaling (schedule.tf) e na revisão é o ci/deploy.sh.
  desired_count = 0

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn   = aws_service_discovery_service.backend.arn
    container_name = "web"
    container_port = 8000
  }

  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }
}

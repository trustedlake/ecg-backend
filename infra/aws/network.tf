# VPC própria sem NAT nem endpoints: tasks em subnet pública (IP público só para saída,
# SG sem entrada da internet) e RDS em subnet privada sem rota para fora.

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Sem rota 0.0.0.0/0: só a rota local da VPC.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Security groups (regras em recursos separados para evitar ciclo entre SGs) ---

resource "aws_security_group" "vpclink" {
  name        = "${local.name}-vpclink"
  description = "VPC Link do API Gateway: sem entrada; sai so para o container web"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-vpclink" }
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "Tasks do ECS: entrada 8000 so do VPC Link"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-ecs" }
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "RDS: entrada 5432 so das tasks do ECS"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-rds" }
}

resource "aws_vpc_security_group_egress_rule" "vpclink_to_ecs" {
  security_group_id            = aws_security_group.vpclink.id
  referenced_security_group_id = aws_security_group.ecs.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "Container web"
}

resource "aws_vpc_security_group_ingress_rule" "ecs_from_vpclink" {
  security_group_id            = aws_security_group.ecs.id
  referenced_security_group_id = aws_security_group.vpclink.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "API Gateway via VPC Link"
}

# 443 para qualquer destino: ECR, S3, SQS, Lambda, SSM e Logs por endpoints públicos (sem NAT/endpoints).
resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "APIs da AWS"
}

resource "aws_vpc_security_group_egress_rule" "ecs_to_rds" {
  security_group_id            = aws_security_group.ecs.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL"
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_ecs" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.ecs.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Tasks do ECS"
}

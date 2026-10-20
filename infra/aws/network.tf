# Own VPC with no NAT or endpoints: tasks in a public subnet (public IP for egress only,
# SG with no ingress from the internet) and RDS in a private subnet with no outbound route.

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

# No 0.0.0.0/0 route: only the local VPC route.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Security groups (rules in separate resources to avoid cycles between SGs) ---

resource "aws_security_group" "vpclink" {
  name        = "${local.name}-vpclink"
  description = "API Gateway VPC Link: no ingress; egress only to the web container"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-vpclink" }

  # Description is ForceNew; ignore it so a text change never replaces the live SG.
  lifecycle {
    ignore_changes = [description]
  }
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "ECS tasks: ingress 8000 only from the VPC Link"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-ecs" }

  # Description is ForceNew; ignore it so a text change never replaces the live SG.
  lifecycle {
    ignore_changes = [description]
  }
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "RDS: ingress 5432 only from the ECS tasks"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.name}-rds" }

  # Description is ForceNew; ignore it so a text change never replaces the live SG.
  lifecycle {
    ignore_changes = [description]
  }
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

# 443 to any destination: ECR, S3, SQS, Lambda, SSM and Logs through public endpoints (no NAT/endpoints).
resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "AWS APIs"
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

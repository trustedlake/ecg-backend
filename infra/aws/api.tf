# Entrada pública: API Gateway HTTP API (HTTPS do execute-api) → VPC Link → Cloud Map → task `web`.
# Sem ALB/NLB e sem CloudFront. A mesma URL serve /api, /admin e o app web (export do Expo).

resource "aws_apigatewayv2_api" "this" {
  name          = "ecg-backend"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_vpc_link" "this" {
  name               = local.name
  subnet_ids         = aws_subnet.public[*].id
  security_group_ids = [aws_security_group.vpclink.id]
}

resource "aws_apigatewayv2_integration" "backend" {
  api_id             = aws_apigatewayv2_api.this.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = aws_service_discovery_service.backend.arn
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.this.id

  payload_format_version = "1.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.backend.id}"
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/ecg/api-access"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 20
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId          = "$context.requestId"
      ip                 = "$context.identity.sourceIp"
      requestTime        = "$context.requestTime"
      method             = "$context.httpMethod"
      path               = "$context.path"
      status             = "$context.status"
      responseLength     = "$context.responseLength"
      latencyMs          = "$context.responseLatency"
      integrationStatus  = "$context.integrationStatus"
      integrationLatency = "$context.integrationLatency"
      integrationError   = "$context.integrationErrorMessage"
      userAgent          = "$context.identity.userAgent"
    })
  }
}

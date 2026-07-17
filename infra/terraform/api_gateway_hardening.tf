# ============================================================
# Connect HTTP API origin-bypass hardening
# ============================================================
# The API and `$default` stage pre-date Terraform. Declarative imports make the
# existing objects always-present resources; they must be adopted in the same
# reviewed runtime migration that disables the public execute-api endpoint.
# The custom-domain mapping is a separate API Gateway object and is deliberately
# not recreated here. terraform_runtime_guard.sh snapshots its exact
# connect.newstv.co.jp -> esk97z9grh/$default mapping before plan and verify, so
# the in-place API/stage update cannot silently strand the supported endpoint.

locals {
  connect_http_api_id = "esk97z9grh"
  connect_http_api_access_log_format = jsonencode({
    requestId          = "$context.requestId"
    routeKey           = "$context.routeKey"
    status             = "$context.status"
    responseLength     = "$context.responseLength"
    integrationStatus  = "$context.integration.status"
    integrationLatency = "$context.integrationLatency"
    responseType       = "$context.error.responseType"
  })
}

resource "aws_cloudwatch_log_group" "connect_http_api_access" {
  name              = "/aws/apigateway/${var.project_name}-${var.environment}-connect-web"
  retention_in_days = 90

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_apigatewayv2_api" "connect_web" {
  name          = "${var.project_name}-connectweb-api"
  protocol_type = "HTTP"

  # Only the custom domain/Cloudflare route may reach this API.
  disable_execute_api_endpoint = true

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

resource "aws_apigatewayv2_stage" "connect_web_default" {
  api_id      = aws_apigatewayv2_api.connect_web.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.connect_http_api_access.arn
    # Intentionally excludes raw path/query, headers, identity, authorizer
    # context, request/response bodies, and OAuth/HMAC material.
    format = local.connect_http_api_access_log_format
  }

  default_route_settings {
    detailed_metrics_enabled = false
  }

  depends_on = [
    aws_cloudwatch_log_group.connect_http_api_access,
    terraform_data.runtime_guard,
  ]

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

import {
  to = aws_apigatewayv2_api.connect_web
  id = "esk97z9grh"
}

import {
  to = aws_apigatewayv2_stage.connect_web_default
  id = "esk97z9grh/$default"
}

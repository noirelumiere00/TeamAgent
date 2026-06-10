# ============================================================
# CloudWatch — Fargate(OpenClaw/MCP) 観測・アラーム・ダッシュボード（§I / N1・M4）
# ============================================================
# 既存 cloudwatch.tf の SNS topic(aws_sns_topic.alarms)・namespace(local.metric_namespace)・
# パターンをそのまま流用。対象 log group は fargate.tf の /teamagent/<env>/{openclaw,teamagent-mcp}。
# 構造化ログ(structlog JSON Lines) の event 名: identity_spoof_rejected / mcp_tool_error / cost_usd。

# ---------- metric filters（MCP バックエンドログ） ----------
resource "aws_cloudwatch_log_metric_filter" "mcp_spoof_rejected" {
  name           = "${var.project_name}-${var.environment}-mcp-spoof-rejected"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.event = \"identity_spoof_rejected\" }"

  metric_transformation {
    name          = "McpIdentitySpoofRejected"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "mcp_tool_error" {
  name           = "${var.project_name}-${var.environment}-mcp-tool-error"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.event = \"mcp_tool_error\" || $.level = \"error\" }"

  metric_transformation {
    name          = "McpToolError"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "mcp_cost_usd" {
  name           = "${var.project_name}-${var.environment}-mcp-cost-usd"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.cost_usd = * }"

  metric_transformation {
    name          = "McpCostUSD"
    namespace     = local.metric_namespace
    value         = "$.cost_usd"
    default_value = "0"
    unit          = "None"
  }
}

# ---------- alarms ----------
# なりすまし拒否は「攻撃 or バグの早期検知」シグナル＝5分窓で1件でも通知。
resource "aws_cloudwatch_metric_alarm" "mcp_spoof_rejected" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-identity-spoof-rejected"
  alarm_description   = "MCP境界で identity 詐称/不整合を拒否（攻撃 or 結線バグの兆候）"
  namespace           = local.metric_namespace
  metric_name         = "McpIdentitySpoofRejected"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "mcp_tool_error_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-tool-error-spike"
  alarm_description   = "MCP tool 実行エラーが 5 分窓で ${var.error_count_threshold} 件以上"
  namespace           = local.metric_namespace
  metric_name         = "McpToolError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.error_count_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "mcp_daily_cost_high" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-daily-cost-high"
  alarm_description   = "MCP 経由 Bedrock 日次コストが ${var.daily_cost_threshold_usd} USD を超過"
  namespace           = local.metric_namespace
  metric_name         = "McpCostUSD"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.daily_cost_threshold_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# ---------- dashboard ----------
resource "aws_cloudwatch_dashboard" "fargate" {
  dashboard_name = "${var.project_name}-${var.environment}-openclaw-pilot"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "MCP cost (USD, sum/5m) & spoof-rejected / tool-error"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            [local.metric_namespace, "McpCostUSD", { stat = "Sum", label = "cost_usd" }],
            [local.metric_namespace, "McpIdentitySpoofRejected", { stat = "Sum", label = "spoof_rejected" }],
            [local.metric_namespace, "McpToolError", { stat = "Sum", label = "tool_error" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "ECS CPU/Memory utilized (Container Insights)"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            ["ECS/ContainerInsights", "CpuUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-openclaw", { stat = "Average", label = "openclaw cpu" }],
            ["ECS/ContainerInsights", "CpuUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-mcp", { stat = "Average", label = "mcp cpu" }],
            ["ECS/ContainerInsights", "MemoryUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-mcp", { stat = "Average", label = "mcp mem" }],
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 6
        width  = 24
        height = 6
        properties = {
          title  = "MCP recent identity & errors"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.mcp.name}' | fields @timestamp, event, tool, reason, slack_user_id_audit | filter event in ['identity_company_shared','identity_spoof_rejected','mcp_tool_error'] | sort @timestamp desc | limit 50"
        }
      },
    ]
  })
}

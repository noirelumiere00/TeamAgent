# ============================================================
# CloudWatch メトリクスフィルタ + アラーム
# Sprint 2 / 2.6 観測・運用基盤
# ============================================================
# 前提：
#   - structured_log は JSON Lines（structlog で出力）
#   - 各レコードに cost_usd / latency_ms が含まれる（CLAUDE.md 6-bis）
#   - エラーは "search_skill_failed" or level=error で出る
#
# メトリクス名前空間: TeamAgent/<env>
# ============================================================

locals {
  metric_namespace = "${var.project_name}/${var.environment}"
}

# ---------- SNS Topic（メール通知）----------
resource "aws_sns_topic" "alarms" {
  name = "${var.project_name}-${var.environment}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  for_each = toset(var.alarm_email_endpoints)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---------- メトリクスフィルタ ----------
# 1) コスト：各 Bedrock 呼び出しの cost_usd を集計
resource "aws_cloudwatch_log_metric_filter" "cost_usd" {
  name           = "${var.project_name}-${var.environment}-cost-usd"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.cost_usd = * }"

  metric_transformation {
    name          = "BedrockCostUSD"
    namespace     = local.metric_namespace
    value         = "$.cost_usd"
    default_value = "0"
    unit          = "None"
  }
}

# 2) レイテンシ：Skill 実行の latency_ms
resource "aws_cloudwatch_log_metric_filter" "latency_ms" {
  name           = "${var.project_name}-${var.environment}-latency-ms"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.latency_ms = * }"

  metric_transformation {
    name          = "SkillLatencyMs"
    namespace     = local.metric_namespace
    value         = "$.latency_ms"
    default_value = "0"
    unit          = "Milliseconds"
  }
}

# 3) エラー：例外 or 明示エラーイベント
resource "aws_cloudwatch_log_metric_filter" "error_count" {
  name           = "${var.project_name}-${var.environment}-error-count"
  log_group_name = aws_cloudwatch_log_group.app.name
  # structlog の level=error or 既知エラーイベント名を捕捉
  pattern = "{ $.level = \"error\" || $.event = \"search_skill_failed\" }"

  metric_transformation {
    name          = "ErrorCount"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# ---------- アラーム ----------
# 日次コスト > 閾値（24h SUM）
resource "aws_cloudwatch_metric_alarm" "daily_cost_high" {
  alarm_name          = "${var.project_name}-${var.environment}-daily-bedrock-cost-high"
  alarm_description   = "Bedrock 日次コストが ${var.daily_cost_threshold_usd} USD を超過"
  namespace           = local.metric_namespace
  metric_name         = "BedrockCostUSD"
  statistic           = "Sum"
  period              = 86400 # 24h
  evaluation_periods  = 1
  threshold           = var.daily_cost_threshold_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# p95 latency > 閾値（15min 窓）
resource "aws_cloudwatch_metric_alarm" "p95_latency_high" {
  alarm_name          = "${var.project_name}-${var.environment}-p95-latency-high"
  alarm_description   = "Skill 応答の p95 latency が ${var.p95_latency_threshold_ms} ms を超過"
  namespace           = local.metric_namespace
  metric_name         = "SkillLatencyMs"
  extended_statistic  = "p95"
  period              = 900 # 15min
  evaluation_periods  = 2
  threshold           = var.p95_latency_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# エラー 5 分窓で N 件以上
resource "aws_cloudwatch_metric_alarm" "error_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-error-spike"
  alarm_description   = "5xx / 例外が 5 分窓で ${var.error_count_threshold} 件以上"
  namespace           = local.metric_namespace
  metric_name         = "ErrorCount"
  statistic           = "Sum"
  period              = 300 # 5min
  evaluation_periods  = 1
  threshold           = var.error_count_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

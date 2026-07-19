# ============================================================
# Outputs — Fargate(OpenClaw/MCP) read-only運用照合
# ============================================================
# service/log/dashboardの照合用。direct ECS mutationの案内には使用しない。

output "ecs_cluster_name" {
  description = "ECSクラスタ名（read-only照合用。変更は正準saved-plan flowのみ）"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_openclaw" {
  description = "OpenClawサービス名。rollbackはdurable previous task revision、ECS circuit breaker、fresh authorizationを使う正準saved-plan runbookに従い、direct ECS操作は行わない。"
  value       = "${var.project_name}-${var.environment}-openclaw"
}

output "ecs_service_mcp" {
  description = "TeamAgent-MCP サービス名"
  value       = "${var.project_name}-${var.environment}-mcp"
}

output "log_group_openclaw" {
  description = "OpenClaw の CloudWatch Logs group"
  value       = aws_cloudwatch_log_group.openclaw.name
}

output "log_group_mcp" {
  description = "TeamAgent-MCP の CloudWatch Logs group"
  value       = aws_cloudwatch_log_group.mcp.name
}

output "cloudwatch_dashboard" {
  description = "パイロット観測ダッシュボード名"
  value       = aws_cloudwatch_dashboard.fargate.dashboard_name
}

output "alarms_sns_topic_arn" {
  description = "アラーム通知先 SNS topic ARN（既存・cloudwatch.tf）"
  value       = aws_sns_topic.alarms.arn
}

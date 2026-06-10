# ============================================================
# Outputs — Fargate(OpenClaw/MCP) 運用参照（§I / N1）
# ============================================================
# apply 後の `aws ecs` 操作・ロールバック・dashboard 参照のための名前を露出（ハードコード回避）。

output "ecs_cluster_name" {
  description = "ECS クラスタ名（aws ecs update-service 等で使用）"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_openclaw" {
  description = "OpenClaw 外殻サービス名（ロールバック: --desired-count 0）"
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

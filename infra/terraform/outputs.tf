output "db_endpoint" {
  description = "RDS エンドポイント"
  value       = aws_db_instance.main.endpoint
}

output "db_name" {
  description = "DB 名"
  value       = var.db_name
}

output "db_password_secret_arn" {
  description = "DB パスワードを保管している Secrets Manager の ARN"
  value       = aws_secretsmanager_secret.db_password.arn
  sensitive   = true
}

output "s3_raw_bucket" {
  description = "生ファイル保管用 S3 バケット"
  value       = aws_s3_bucket.raw_files.id
}

output "lambda_role_arn" {
  description = "Lambda 実行ロール ARN"
  value       = aws_iam_role.lambda_exec.arn
}

output "cloudwatch_log_group" {
  description = "アプリケーションログの CloudWatch ロググループ"
  value       = aws_cloudwatch_log_group.app.name
}

variable "aws_region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "environment" {
  description = "環境名 (dev / staging / prod)"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "プロジェクトプレフィックス"
  type        = string
  default     = "teamagent"
}

# ---------- RDS / pgvector ----------
variable "db_instance_class" {
  description = "RDS インスタンスクラス"
  type        = string
  default     = "db.t4g.micro" # Dev/PoC 用（≒$15/月）。本番は db.r7g.large を想定
}

variable "db_allocated_storage" {
  description = "RDS ストレージ (GB)"
  type        = number
  default     = 20
}

variable "db_engine_version" {
  description = "PostgreSQL バージョン（2026/5/22 時点 AWS RDS 利用可能の最新マイナー）"
  type        = string
  default     = "16.14"
}

variable "db_username" {
  description = "RDS マスターユーザー名"
  type        = string
  default     = "teamagent"
}

variable "db_name" {
  description = "RDS データベース名"
  type        = string
  default     = "teamagent"
}

variable "db_multi_az" {
  description = "Multi-AZ 配置 (本番 true 推奨)"
  type        = bool
  default     = false
}

# ---------- Lambda / Bedrock ----------
variable "bedrock_model_id" {
  description = "Bedrock で利用する Claude モデル ID（東京リージョン推論プロファイル）"
  type        = string
  default     = "jp.anthropic.claude-sonnet-4-6"
}

variable "lambda_memory_size" {
  description = "Lambda メモリサイズ (MB)"
  type        = number
  default     = 1024
}

variable "lambda_timeout" {
  description = "Lambda タイムアウト (秒)"
  type        = number
  default     = 300 # Agent ループは長くなりがち
}

# ---------- 観測・アラーム閾値（Sprint 2 / 2.6）----------
variable "alarm_email_endpoints" {
  description = "アラーム通知先メールアドレス（最初は SNS でメール、後で Chatbot/Slack に拡張）"
  type        = list(string)
  default     = [] # apply 時に tfvars で上書き
}

variable "daily_cost_threshold_usd" {
  description = "日次 Bedrock コスト（USD）警告閾値。これを超えると CloudWatch アラーム発火"
  type        = number
  default     = 5.0
}

variable "p95_latency_threshold_ms" {
  description = "Slack 応答の p95 レイテンシ（ms）警告閾値"
  type        = number
  default     = 15000
}

variable "error_count_threshold" {
  description = "5xx / 例外の 5 分窓カウント警告閾値"
  type        = number
  default     = 3
}

# ---------- セキュリティ（Sprint 2 / 2.7）----------
variable "enable_cloudtrail" {
  description = "CloudTrail multi-region trail を作成する（既存があるなら false）"
  type        = bool
  default     = true
}

variable "enable_iam_access_analyzer" {
  description = "IAM Access Analyzer をアカウント単位で有効化する"
  type        = bool
  default     = true
}

variable "enable_bedrock_invocation_logging" {
  description = "Bedrock invocation logging を S3 + KMS で有効化する（コンソール / Terraform で 1 アカウント 1 設定）"
  type        = bool
  default     = true
}

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
  default     = "db.t4g.medium"  # PoC 用。本番は db.r7g.large を想定
}

variable "db_allocated_storage" {
  description = "RDS ストレージ (GB)"
  type        = number
  default     = 50
}

variable "db_engine_version" {
  description = "PostgreSQL バージョン"
  type        = string
  default     = "16.4"
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
  description = "Bedrock で利用する Claude モデル ID"
  type        = string
  default     = "anthropic.claude-sonnet-4-6-20250719-v1:0"
}

variable "lambda_memory_size" {
  description = "Lambda メモリサイズ (MB)"
  type        = number
  default     = 1024
}

variable "lambda_timeout" {
  description = "Lambda タイムアウト (秒)"
  type        = number
  default     = 300  # Agent ループは長くなりがち
}

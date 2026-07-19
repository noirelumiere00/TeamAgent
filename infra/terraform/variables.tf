variable "aws_region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"

  validation {
    condition     = var.aws_region == "ap-northeast-1"
    error_message = "このS3 backend/stateはTeamAgent dev東京リージョン専用です。"
  }
}

variable "environment" {
  description = "環境名 (dev / staging / prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = var.environment == "dev"
    error_message = "このS3 backend/stateはTeamAgent dev環境専用です。"
  }
}

variable "project_name" {
  description = "プロジェクトプレフィックス"
  type        = string
  default     = "teamagent"

  validation {
    condition     = var.project_name == "teamagent"
    error_message = "このS3 backend/stateはTeamAgent dev project専用です。"
  }
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

# ---------- S3 ライフサイクル（提案/レポート成果物の保管コスト最適化・監視#20）----------
# 提案 PDF/PPTX/HTML レポートは生成直後だけ高頻度アクセス、その後は監査/再利用の低頻度に移行する。
# Glacier Instant Retrieval（ミリ秒取得・長期保管で安価）へ自動遷移してストレージ費を抑える。
variable "s3_glacier_transition_days" {
  description = "raw-files の現行オブジェクトを Glacier Instant Retrieval へ遷移するまでの日数"
  type        = number
  default     = 90
}

variable "s3_noncurrent_expiration_days" {
  description = "バージョニングの非現行バージョンを削除するまでの日数（履歴肥大の抑制）"
  type        = number
  default     = 180
}

# ---------- Lambda / Bedrock ----------
variable "bedrock_model_id" {
  description = "Bedrock で利用する Claude モデル ID（東京リージョン推論プロファイル）。コスト方針によりスキル/オーケストレーター既定を Haiku 4.5 に固定（2026-06-29）。"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition = contains([
      "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      "jp.anthropic.claude-sonnet-4-6",
    ], var.bedrock_model_id)
    error_message = "Lambda Bedrock modelは監査済みJP Haiku 4.5またはSonnet 4.6 profileだけを使用できます。"
  }
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
  description = "canonical SNS topicの確認済み通知先。UTF-8 byte列exact s-komata@vectorinc.co.jp 1件だけ。"
  type        = list(string)
  default     = ["s-komata@vectorinc.co.jp"]
  sensitive   = true

  validation {
    condition = (
      length(var.alarm_email_endpoints) == 1 &&
      var.alarm_email_endpoints[0] == "s-komata@vectorinc.co.jp"
    )
    error_message = "alarm_email_endpointsはraw byte exact s-komata@vectorinc.co.jp 1件だけを指定してください（trim/lower不可）。"
  }
}

variable "alarm_chatbot_configuration_arns" {
  description = "canonical SNS topicのChatbot modeは禁止。互換入力として空listだけを受け付ける。"
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.alarm_chatbot_configuration_arns) == 0
    error_message = "alarm_chatbot_configuration_arnsは空である必要があります。approved email以外のChatbot modeは禁止です。"
  }
}

variable "require_alarm_delivery" {
  description = "本番runtime/schedule rolloutを、approved exact emailの確認済みexclusive subscriptionが無い場合にfail-closedする。guarded planは常にtrueを強制する。"
  type        = bool
  default     = true
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
  description = "CloudTrail用S3 bucket/policy/versioningとmulti-region trailを管理する"
  type        = bool
  default     = true
}

variable "enable_cloudtrail_log_delivery" {
  description = "CloudTrail producerを有効にする。新規bucketで初めてversioningを有効化する場合はfalseで基盤だけを先に適用し、15分の伝播待ち後にtrueへ変更する"
  type        = bool
  default     = true
}

variable "enable_iam_access_analyzer" {
  description = "IAM Access Analyzer をアカウント単位で有効化する"
  type        = bool
  default     = true
}

variable "enable_bedrock_invocation_logging" {
  description = "Bedrock invocation logging用S3 bucket/policy/versioningとリージョン×アカウント設定を管理する"
  type        = bool
  default     = true
}

variable "enable_bedrock_invocation_log_delivery" {
  description = "Bedrock invocation log producerを有効にする。新規bucketではversioning伝播待ち完了後にtrueへ変更する"
  type        = bool
  default     = true
}

variable "bedrock_logs_retention_days" {
  description = "Bedrock AI入出力ログのbedrock/ prefixに適用する最低保持日数。current/noncurrentのどのversionも生成後60日未満では削除しない固定契約"
  type        = number
  default     = 60

  validation {
    condition     = var.bedrock_logs_retention_days == 60
    error_message = "Bedrock AI入出力ログの保持期間は承認済みの60日だけを指定できます。"
  }
}

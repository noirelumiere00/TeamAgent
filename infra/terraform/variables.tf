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
  description = "canonical SNS topicの確認済み通知先メール。state移行を決定的にするため、この環境では0または1件。"
  type        = list(string)
  default     = []

  validation {
    condition = (
      length(var.alarm_email_endpoints) <= 1 &&
      length(distinct([
        for endpoint in var.alarm_email_endpoints :
        lower(trimspace(endpoint))
      ])) == length(var.alarm_email_endpoints) &&
      alltrue([
        for endpoint in var.alarm_email_endpoints :
        can(regex(
          "^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$",
          trimspace(endpoint),
        ))
      ])
    )
    error_message = "alarm_email_endpointsは重複のない有効なメールアドレスを最大1件指定してください。"
  }
}

variable "alarm_chatbot_configuration_arns" {
  description = "canonical SNS topicへ接続済みのAmazon Q Developer in chat applications (AWS Chatbot) configuration ARN。別state所有のexact ARNのみ。"
  type        = list(string)
  default     = []

  validation {
    condition = (
      length(distinct(var.alarm_chatbot_configuration_arns)) ==
      length(var.alarm_chatbot_configuration_arns) &&
      alltrue([
        for arn in var.alarm_chatbot_configuration_arns :
        can(regex(
          "^arn:aws:chatbot::718959508629:chat-configuration/(slack-channel|microsoft-teams-channel)/[A-Za-z0-9._-]+$",
          arn,
        ))
      ])
    )
    error_message = "alarm_chatbot_configuration_arnsはaccount 718959508629のexact Slack/Teams chat-configuration ARNである必要があります。"
  }
}

variable "require_alarm_delivery" {
  description = "本番runtime/schedule rolloutを、確認済みemailまたはchat integrationが無い場合にfail-closedする。guarded planは常にtrueを強制する。"
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

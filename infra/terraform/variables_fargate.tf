# ============================================================
# 変数 — ECS Fargate（P1: OpenClaw外殻 + TeamAgent-MCP境界）§H / M2
# ============================================================
# 値は terraform.tfvars / 環境で上書き。秘密「値」はここに書かない（secret 名のみ）。

variable "openclaw_image" {
  description = "OpenClaw外殻イメージ（ECR・digest pin推奨。例: <url>@sha256:...）。空ならservice未作成相当"
  type        = string
  default     = ""
}

variable "mcp_image" {
  description = "TeamAgent-MCP バックエンドイメージ（ECR・digest pin推奨）"
  type        = string
  default     = ""
}

variable "fargate_mcp_cpu" {
  description = "MCPタスク CPU units（1024=1vCPU）"
  type        = number
  default     = 1024
}

variable "fargate_mcp_memory" {
  description = "MCPタスク メモリ MB（embedder/torch を載せるため余裕）"
  type        = number
  default     = 4096
}

variable "fargate_openclaw_cpu" {
  description = "OpenClawタスク CPU units"
  type        = number
  default     = 512
}

variable "fargate_openclaw_memory" {
  description = "OpenClawタスク メモリ MB"
  type        = number
  default     = 1024
}

variable "shared_company_domains" {
  description = "会社共有モデル(§G)の会社ドメイン（カンマ区切り）。MCPへ TEAMAGENT_SHARED_COMPANY_DOMAINS で渡す。例: vectorinc.co.jp"
  type        = string
  default     = ""
}

variable "openclaw_model_id" {
  # §J: 既定を openclaw.config.json5（焼込・権威）と一致させる。本 var は参照/将来の env 注入用。
  # §P: list-inference-profiles 実測値で確定（2026-06-11・account 718959508629/東京）。
  description = "OpenClaw外側モデル（Haiku4.5・東京推論プロファイル実ID）。権威=openclaw.config.json5"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
}

# ---------- 秘密値の secret 名（本人が Secrets Manager に作成。値は注入のみ） ----------
variable "mcp_bearer_secret_name" {
  description = "OpenClaw⟷MCP 共有 bearer の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/mcp/bearer"
}

variable "database_url_secret_name" {
  description = "MCP が使う DATABASE_URL（pgvector）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/database-url"
}

variable "slack_bot_token_secret_name" {
  description = "OpenClaw用 Slack Bot Token（xoxb）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/slack-bot-token"
}

variable "slack_app_token_secret_name" {
  description = "OpenClaw用 Slack App Token（xapp・Socket Mode）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/slack-app-token"
}

variable "openclaw_gateway_token_secret_name" {
  description = "OpenClaw gateway 管理トークン（full operator scope相当）の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/openclaw/gateway-token"
}

# §M: スクレイプ/動画ツール（USE_VIDEO_TOOLS/USE_TIKTOK_TOOLS）を有効化する“拡張版”の配線。
# true で Gemini secret 注入・S3(vseo-reports)権限・VSEO_REPORT_BUCKET を MCP タスクへ足す。既定 false＝P1薄殻は不要。
variable "enable_scrape_tools" {
  description = "スクレイプ/動画ツールを有効化（Gemini secret/S3権限/VSEO_REPORT_BUCKETを配線）。Dockerは WITH_SCRAPE_TOOLS=true でビルド。"
  type        = bool
  default     = false
}

variable "video_algo_max_videos" {
  description = "video_algorithm の既定分析本数（5〜10）。同期処理なので OpenClaw timeout(300s)と整合させること。値変更は taskdef env 差し替えのみで反映可。"
  type        = string
  default     = "5"
}

# §M改(VSEO有効化): Gemini 認証は本番EC2と同方式の Vertex SA を採用（API キー方式は廃止）。
variable "vertex_sa_secret_name" {
  description = "Vertex AI SA JSON の secret 名（本番EC2 load_secrets.sh と同一ソース・enable_scrape_tools=true 時に VERTEX_SA_JSON として注入）"
  type        = string
  default     = "teamagent/dev/vertex_sa"
}

variable "gemini_vertex_project" {
  description = "Vertex AI の GCP プロジェクトID（本番 .env.production と同値）"
  type        = string
  default     = "ntv-ai"
}

variable "gemini_vertex_location" {
  description = "Vertex AI ロケーション（本番 .env.production と同値）"
  type        = string
  default     = "us-central1"
}

variable "enable_vpc_endpoints" {
  description = "VPC interface endpoint（bedrock/secrets/kms/ecr/logs）を作成して egress を private 化する"
  type        = bool
  default     = true
}

# ============================================================
# 変数 — ECS Fargate（P1: OpenClaw外殻 + TeamAgent-MCP境界）§H / M2
# ============================================================
# 値は terraform.tfvars / 環境で上書き。秘密「値」はここに書かない（secret 名のみ）。

variable "openclaw_image" {
  description = "Guarded saved-plan flowでのみ変更できるOpenClaw release repositoryのdigest URI。空値によるmanaged service/task削除は禁止"
  type        = string

  validation {
    condition = can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-openclaw@sha256:[0-9a-f]{64}$",
      var.openclaw_image,
    ))
    error_message = "openclaw_image must be a nonempty fixed release-repository digest; decommissioning requires a separately reviewed destructive workflow."
  }
}

variable "mcp_image" {
  description = "TeamAgent-MCP release repositoryの必須digest URI。空値によるmanaged service/task削除は禁止"
  type        = string

  validation {
    condition = can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-mcp@sha256:[0-9a-f]{64}$",
      var.mcp_image,
    ))
    error_message = "mcp_image must be a nonempty fixed release-repository digest; decommissioning requires a separately reviewed destructive workflow."
  }
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

variable "mcp_model_id" {
  description = "mcp（スキル/オーケストレーター）の Bedrock モデル ID。コスト方針(2026-06-29)により Haiku 4.5 既定・live rev40 と同値。var.bedrock_model_id を流用しない（あちらは Lambda 用で tfvars が Sonnet を指定しており、共有すると apply で mcp が Sonnet 化する＝2026-07-11 反対尋問レビューで検出）。"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = var.mcp_model_id == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    error_message = "MCPは監査済みJP Claude Haiku 4.5 inference profileだけを使用できます。"
  }
}

variable "enable_proposal_builder" {
  description = "Gemini v3→RAG→統合FMT→Slack添付の proposal_builder をMCPで有効化する。S3固定version資産、KMS権限、generic media workerが全て必須。"
  type        = bool
  default     = false
}

variable "proposal_builder_publish_ready" {
  description = "ready提案書のS3公開(presigned URL発行)を許可する。既定OFF＝Slack添付のみ。"
  type        = bool
  default     = false
}

variable "proposal_builder_deliver_internal_drafts" {
  description = "draft(裏取り前)提案書の社内Slack添付を許可する。既定OFF＝ready以外は添付しない。"
  type        = bool
  default     = false
}

variable "proposal_builder_sync_runtime_verified" {
  description = "代表143MB級でRAG→LLM→render→Slack uploadがOpenClaw共有deadline内に完走したことを受入環境で確認済みならtrue"
  type        = bool
  default     = false
}

variable "proposal_builder_template_s3_bucket" {
  description = "統合FMTのversioning/SSE-KMS有効S3 bucket（enable_proposal_builder時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_key" {
  description = "統合FMT object key（enable_proposal_builder時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_version_id" {
  description = "統合FMTのimmutable S3 VersionId。null/latestは禁止"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_sha256" {
  description = "統合FMT全体のSHA-256 hex"
  type        = string
  default     = ""
}

variable "proposal_builder_template_s3_size" {
  description = "統合FMTの実byte数（1〜256MiB）"
  type        = number
  default     = 0
}

variable "proposal_builder_account_s3_bucket" {
  description = "改変禁止アカウントDBのversioning/SSE-KMS有効S3 bucket（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_key" {
  description = "改変禁止アカウントDB object key（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_version_id" {
  description = "アカウントDBのimmutable S3 VersionId。null/latestは禁止"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_sha256" {
  description = "アカウントDB全体のSHA-256 hex"
  type        = string
  default     = ""
}

variable "proposal_builder_account_s3_size" {
  description = "アカウントDBの実byte数（1〜5MiB）"
  type        = number
  default     = 0
}

variable "proposal_builder_assets_kms_key_arn" {
  description = "統合FMTとアカウントDBを暗号化する単一KMS key ARN（enable時必須）"
  type        = string
  default     = ""
}

variable "proposal_builder_news_channel_id" {
  description = "ingestで実測した general_news-tv のSlack channel ID。未設定時はchannel_nameだけで絞る"
  type        = string
  default     = ""
}

variable "use_calendar_event_tool" {
  description = "calendar_event tool（📅カレンダー登録ボタンの押下処理・v0.3 Task3）を mcp で有効化。既定 false。ON 後に morning_digest_calendar_button=true にする（順序を守らないと無反応ボタン）。"
  type        = bool
  default     = false
}

variable "use_schedule_propose_tool" {
  description = "schedule_propose tool（🗓日程候補提案ボタンの押下処理・v0.3 Task4）を mcp で有効化。既定 false。ON 後に morning_digest_schedule_button=true にする（順序を守らないと無反応ボタン）。"
  type        = bool
  default     = false
}

variable "enable_progress_notify" {
  description = "ツール実行中の進捗表示（v0.3.1 Task7）。ON で mcp が重いツール実行前に『📂 資料を検索しています…』等を Slack へ投稿し完了後に削除する。既定 false。fail-open（送信失敗はツール実行を阻害しない）。bot の chat:write/im:write scope 前提。"
  type        = bool
  default     = false
}

variable "use_entity_tags" {
  description = "名寄せタグ（cls_entities・v0.3.1）。ON で ingest 分類時に Haiku で取引先/代理店/ブランド/コラボ名を抽出し cls_entities に保持（親クライアント検索で子コラボが出る）。既定 false。既存分は scripts/backfill_entities.py で別途 backfill。"
  type        = bool
  default     = false
}

variable "use_ailavault_deeplinks" {
  description = "AiLaVault ディープリンク（v0.3 Task6）。ON で mcp が検索結果に /app#client:<名前> リンクを注入し、app.html の applyHashTarget が該当ノートを自動展開する。既定 false。⚠️ONの前提: connect-web が実 app.html を配信中であること（healthz app_html_source=s3。プレースホルダ配信中にONにすると飛べないリンクになる）。"
  type        = bool
  default     = false
}

variable "enable_report_shorturl" {
  description = "レポート短縮リンク(/r)を発行するか（Part2 段階ゲート＝env USE_REPORT_SHORTURL）。既定 false＝従来 presigned。ON の前提: connect-web が同一新イメージ(/r ルート)＋vseo-s3-read(bootstrap_vseo_s3_iam.sh)を持ち、実機で /r→302 を確認済みであること。揃う前に true にすると受信者側で 404/403 に劣化する。"
  type        = bool
  default     = false
}

variable "video_quota_enabled" {
  description = "動画分析の月間クォータ（v0.3 Task10）。既定 false。有効化前に migration 0017 の本番適用が必須（未適用だと fail-open で素通り・WARN のみ）。"
  type        = bool
  default     = false
}

variable "video_monthly_quota" {
  description = "ユーザーごとの月間動画分析上限（本・JST月次リセット・Step0裁定の初期値20）。"
  type        = number
  default     = 20
}

variable "use_analysis_cache" {
  description = "Gemini 動画分析結果の S3 キャッシュ（v0.3 Task10）。既定 false。同一動画×同一プロンプトの再分析を回避（Gemini=GCP課金の数少ないガード）。"
  type        = bool
  default     = false
}

variable "enable_kaiwai_classify" {
  description = "X投稿者の界隈マルチラベル分類（Part4・env USE_KAIWAI_CLASSIFY）。既定 false＝bio を LLM へ送らず・界隈を尋ねず・カードにチップも出さない（完全 no-op・後方互換）。分類は LLM 推定でありカードには「推定界隈」と明示表示する。⚠️前提1: enable_x_research=true（false 時は USE_KAIWAI_CLASSIFY env 自体が taskdef に出力されず、本フラグ true でも黙って no-op）。⚠️前提2: 第一候補 actor(apidojo) の本番 bio 取得率と分類品質を実データで確認済みであること。"
  type        = bool
  default     = false
}

variable "enable_research_persist" {
  description = "施策研究(x_voice/コメント分析)を pgvector→AiLaVault へ永続記録するか（Part1・env USE_RESEARCH_PERSIST）。既定 false＝完全 no-op。ON の前提: ローカルRDS(SSMトンネル)で admin INSERT した doc を owner/別社員/社外の3者の**通常検索(member接続)**で引き、ACL(会社横断可視・社外不可)が正しいことを実証済みであること（export_vault は admin 接続なので dry-run では RLS 検証にならない）。"
  type        = bool
  default     = false
}

variable "use_payload_offload" {
  description = "MCP 長文ペイロードの S3 退避（v0.3 Task8）。既定 false。対象は会社共有ナレッジ系 tool のみ（allowlist・per-user PII 系は対象外）。"
  type        = bool
  default     = false
}

variable "slack_team_id" {
  description = "本番必須の自社Slack workspace team_id（T + 8文字以上の英大文字/数字）。OpenClaw署名claimとMCP resolverが同じexact IDを検証する。既定の空文字は未設定sentinelでplanをfail-closedにする。"
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^T[A-Z0-9]{8,}$", var.slack_team_id))
    error_message = "slack_team_id is required and must be a canonical Slack T ID."
  }
}

variable "slack_dm_allowlist" {
  description = "本番で必須のSlack DM契約。\"*\"はdmPolicy=open+allowFrom=[\"*\"]、それ以外は1〜100件の重複しないSlack U IDを空白なしのカンマ区切りで指定しdmPolicy=allowlistにする。既定の空文字は安全な未設定sentinelであり、明示値なしのplanをfail-closedにする。"
  type        = string
  default     = ""

  validation {
    condition = (
      var.slack_dm_allowlist == "*" ||
      (
        length(var.slack_dm_allowlist) <= 2048 &&
        can(regex("^U[A-Z0-9]{8,}(,U[A-Z0-9]{8,}){0,99}$", var.slack_dm_allowlist)) &&
        length(distinct(split(",", var.slack_dm_allowlist))) == length(split(",", var.slack_dm_allowlist))
      )
    )
    error_message = "slack_dm_allowlist is required: use \"*\" or 1-100 unique comma-separated Slack U IDs with no spaces."
  }
}

variable "openclaw_model_id" {
  # §J: 既定を openclaw.config.json5（焼込・権威）と一致させる。本 var は参照/将来の env 注入用。
  # §P: list-inference-profiles 実測値で確定（2026-06-11・account 718959508629/東京）。
  description = "OpenClaw外側モデル（Haiku4.5・東京推論プロファイル実ID）。権威=openclaw.config.json5"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = var.openclaw_model_id == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    error_message = "OpenClawは監査済みJP Claude Haiku 4.5 inference profileだけを使用できます。"
  }
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

variable "openclaw_caller_claim_secret_name" {
  description = "OpenClaw→MCPのone-use caller identity claim専用HMAC鍵（bearerとは別、32-byte以上）のSecrets Manager名"
  type        = string
  default     = "teamagent/dev/openclaw/caller-claim-hmac"
}

# §M: スクレイプ/動画ツール（USE_VIDEO_TOOLS/USE_TIKTOK_TOOLS）を有効化する“拡張版”の配線。
# true で Gemini secret 注入・S3(vseo-reports)権限・VSEO_REPORT_BUCKET を MCP タスクへ足す。既定 false＝P1薄殻は不要。
variable "enable_scrape_tools" {
  description = "スクレイプ/動画ツールを有効化（Gemini secret/S3権限/VSEO_REPORT_BUCKETを配線）。Dockerは WITH_SCRAPE_TOOLS=true でビルド。"
  type        = bool
  default     = false
}

variable "video_algo_max_videos" {
  description = "video_algorithm の既定『深掘り分析』本数（5〜10）。同期処理なので OpenClaw timeout(300s)と整合させること。値変更は taskdef env 差し替えのみで反映可。"
  type        = string
  default     = "5"
}

variable "video_algo_board_size" {
  description = "video_algorithm の『取得（上位ボード）』本数（5〜30・既定30）。メタのみ取得＝軽く、深掘り分析(video_algo_max_videos)とは独立。値変更は taskdef env 差し替えのみで反映可。"
  type        = string
  default     = "30"
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

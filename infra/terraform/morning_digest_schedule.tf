# ============================================================
# §U-Part3 Step C: morning_digest を ECS Scheduled Task で起動（平日 9:30 JST）
# ============================================================
# 役割: 平日朝 9:30 JST に EventBridge Scheduled Task で teamagent-mcp image を起動し、
#   scripts/run_morning_digest_fargate.py が RDS oauth_tokens 連携済の各ユーザーに対し
#   MorningDigestSkill を実行→Slack DM (Block Kit) で本人に配信する。
#
# image: 既存 teamagent-mcp の ECR image を流用（teamagent パッケージ同一）。
#   ENTRYPOINT は scripts/run_morning_digest_fargate.py（per-user token store + Slack 配信）。
#
# 選定理由（Plan の Phase 2 評価と同じ）:
#   - Lambda+VPC ENI: 15 分制限・ENI cold start で per-user N 人ループには不向き
#   - ECS Scheduled Task（本実装）: 15 分制限なし・既存 Fargate IAM 流用・ingest_schedule.tf と同パターン

# ---------- 変数 ----------
variable "enable_morning_digest" {
  description = "morning_digest の ECS Scheduled Task（taskdef/EventBridge rule/target/IAM）を有効化"
  type        = bool
  default     = false
}

variable "fargate_morning_digest_cpu" {
  description = "morning_digest タスク CPU（per-user N 人ループ + Bedrock + Slack post）"
  type        = number
  default     = 1024
}

variable "fargate_morning_digest_memory" {
  description = "morning_digest タスク メモリ MB"
  type        = number
  default     = 2048
}

variable "morning_digest_users" {
  description = "対象ユーザーの email リスト（カンマ区切り・空なら RDS oauth_tokens から動的抽出）"
  type        = string
  default     = ""
}

variable "morning_digest_exclude" {
  description = "digest 対象から除外する email リスト（カンマ区切り）。テストユーザーの一時停止など。Google 連携は切らない。"
  type        = string
  default     = ""
}

variable "digest_important_senders" {
  description = "重要送信者（VIP）の email/ドメイン（カンマ区切り）。triage の優先度ヒントに使う。"
  type        = string
  default     = ""
}

variable "digest_internal_domain" {
  description = "社内ドメイン（差出人区分 internal 判定用）。"
  type        = string
  default     = "vectorinc.co.jp"
}

variable "morning_digest_concurrency" {
  description = "1 タスク内で同時処理するユーザー数。1=逐次（既定）。人数増加時に上げ所要時間を短縮。"
  type        = number
  default     = 1
}

variable "morning_digest_slack_unread" {
  description = "Slack 返信漏れ検知（v0.3 Task1）を朝ダイジェストに含める。既定 false（§10 E1-2）。"
  type        = bool
  default     = false
}

variable "morning_digest_schedule_button" {
  description = "🗓日程候補提案ボタン（v0.3 Task4）を朝ダイジェストに描画。既定 false。ON は schedule_propose tool の本番有効化後。"
  type        = bool
  default     = false
}

variable "morning_digest_calendar_button" {
  description = "📅カレンダー登録ボタン（v0.3 Task3）を朝ダイジェストに描画。既定 false。ON は calendar_event tool（USE_CALENDAR_EVENT_TOOL + toolFilter）が本番有効になってから（先に出すと無反応ボタン）。"
  type        = bool
  default     = false
}

variable "morning_digest_model_id" {
  description = "triage/下書き生成に使う Bedrock モデル ID。既定 Haiku（低コスト・高速）。"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "morning_digest_schedule_expression" {
  description = "EventBridge cron 式（既定: 平日 0:30 UTC = 9:30 JST）"
  type        = string
  default     = "cron(30 0 ? * MON-FRI *)"
}

# ---------- CloudWatch Logs ----------
resource "aws_cloudwatch_log_group" "morning_digest" {
  name              = "/${var.project_name}/${var.environment}/morning-digest"
  retention_in_days = 30
}

# ---------- 以降は enable_morning_digest ゲート ----------

# morning_digest は per-user OAuth で gmail/gcalendar/Bedrock を叩く。
# token は RDS oauth_tokens（KMS 暗号化）から取得し、refresh には GOOGLE_CLIENT_ID/SECRET が要る。
# ingest と同じ teamagent/dev/google_oauth (JSON 形式) を再利用する。
data "aws_secretsmanager_secret" "morning_digest_google_oauth" {
  count = var.enable_morning_digest ? 1 : 0
  name  = "teamagent/dev/google_oauth"
}

# --- 実行ロール（launch 時 secrets 注入用） ---
resource "aws_iam_role" "ecs_execution_morning_digest" {
  count              = var.enable_morning_digest ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-morning-digest"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_morning_digest_managed" {
  count      = var.enable_morning_digest ? 1 : 0
  role       = aws_iam_role.ecs_execution_morning_digest[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_morning_digest_secrets" {
  count = var.enable_morning_digest ? 1 : 0
  statement {
    sid     = "ReadMorningDigestSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      data.aws_secretsmanager_secret.database_url.arn,
      data.aws_secretsmanager_secret.slack_bot.arn,
      data.aws_secretsmanager_secret.morning_digest_google_oauth[0].arn,
      # per-user token refresh 用の connect(web 型)クライアント secret（CONNECT_GOOGLE_CLIENT_SECRET）。
      data.aws_secretsmanager_secret.connect_google_client_secret[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_morning_digest_secrets" {
  count  = var.enable_morning_digest ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-morning-digest-secrets"
  role   = aws_iam_role.ecs_execution_morning_digest[0].id
  policy = data.aws_iam_policy_document.ecs_execution_morning_digest_secrets[0].json
}

# --- タスクロール: KMS Decrypt + Bedrock InvokeModel ---
# Slack post は SLACK_BOT_TOKEN で chat.postMessage を叩く（IAM 不要）。
# RDS connect は DATABASE_URL で接続（SG ingress で許可・後述）。
data "aws_iam_policy_document" "morning_digest_task" {
  count = var.enable_morning_digest ? 1 : 0
  statement {
    sid       = "KmsDecryptForOauthTokens"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${local.account_id}:key/*"]
  }
  statement {
    sid = "BedrockInvokeForTriageAndDraft"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = local.bedrock_resources
  }
}

resource "aws_iam_role" "morning_digest_task" {
  count              = var.enable_morning_digest ? 1 : 0
  name               = "${var.project_name}-${var.environment}-morning-digest-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "morning_digest_task" {
  count  = var.enable_morning_digest ? 1 : 0
  name   = "${var.project_name}-${var.environment}-morning-digest-task"
  role   = aws_iam_role.morning_digest_task[0].id
  policy = data.aws_iam_policy_document.morning_digest_task[0].json
}

# --- SG: ingress なし・egress only（Slack/Gmail/Bedrock/RDS/Secrets/KMS への外向き） ---
resource "aws_security_group" "morning_digest" {
  count       = var.enable_morning_digest ? 1 : 0
  name        = "${var.project_name}-${var.environment}-morning-digest-sg"
  description = "morning_digest Scheduled Task (egress only)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-morning-digest-sg" }
}

# RDS への 5432 を morning_digest SG から許可（純加算）
resource "aws_security_group_rule" "db_from_morning_digest" {
  count                    = var.enable_morning_digest ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.morning_digest[0].id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from morning_digest Scheduled Task"
}

# --- Task Definition ---
resource "aws_ecs_task_definition" "morning_digest" {
  count                    = var.enable_morning_digest && var.mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-morning-digest"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_morning_digest_cpu
  memory                   = var.fargate_morning_digest_memory
  execution_role_arn       = aws_iam_role.ecs_execution_morning_digest[0].arn
  task_role_arn            = aws_iam_role.morning_digest_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "morning-digest"
    image     = var.mcp_image
    essential = true
    command   = ["python", "scripts/run_morning_digest_fargate.py"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "STRUCTLOG_FORMAT", value = "json" },
      { name = "MORNING_DIGEST_USERS", value = var.morning_digest_users },
      { name = "MORNING_DIGEST_EXCLUDE", value = var.morning_digest_exclude },
      { name = "IMPORTANT_SENDERS", value = var.digest_important_senders },
      { name = "DIGEST_INTERNAL_DOMAIN", value = var.digest_internal_domain },
      { name = "MORNING_DIGEST_CONCURRENCY", value = tostring(var.morning_digest_concurrency) },
      # triage / 下書き生成に使う Bedrock モデル（既定 Haiku＝低コスト・高速）。
      { name = "BEDROCK_MODEL_ID", value = var.morning_digest_model_id },
      # 下書きは朝に各Gmailスレッドへ自動で作り置き（drafts.create のみ・送信はしない）。ボタンは「確認」導線。
      { name = "DRAFT_ON_DEMAND_ONLY", value = "false" },
      # 作り置きの上限（高重要のみ・コスト抑制。コード既定3→5）。
      { name = "MORNING_DIGEST_MAX_DRAFTS", value = "5" },
      # per-user token のリフレッシュに使う connect(web 型)クライアント ID（secret は下の secrets）。
      { name = "CONNECT_GOOGLE_CLIENT_ID", value = var.connect_google_client_id },
      # OAUTH_KMS_KEY_ID は token store の復号に必要（既存 alias を流用）。
      { name = "OAUTH_KMS_KEY_ID", value = "alias/teamagent-oauth-tokens" },
      { name = "OAUTH_KMS_REGION", value = var.aws_region },
      # Slack 返信漏れ検知（v0.3 Task1・既定OFF）。ON には Slack app の User Token Scopes
      # (search:read 等) 設定＋対象ユーザーの Slack 連携（xoxp・search:read 込み）が前提。
      # 未連携ユーザーは fail-open で空＝段階ロールアウト可。
      { name = "MORNING_DIGEST_SLACK_UNREAD", value = var.morning_digest_slack_unread ? "true" : "false" },
      # 密度優先描画（2026-07-13 パイロットFB「見づらい」対応・env のみで切替可）。
      { name = "MORNING_DIGEST_COMPACT", value = var.morning_digest_compact ? "true" : "false" },
      # 📅カレンダー登録ボタン（v0.3 Task3・既定OFF）。押下先 calendar_event tool の有効化とセットで ON。
      { name = "MORNING_DIGEST_CALENDAR_BUTTON", value = var.morning_digest_calendar_button ? "true" : "false" },
      # 🗓日程候補提案ボタン（v0.3 Task4・既定OFF）。押下先 schedule_propose tool の有効化とセットで ON。
      { name = "MORNING_DIGEST_SCHEDULE_BUTTON", value = var.morning_digest_schedule_button ? "true" : "false" },
      # 予定リマインド（v0.3 Task5・既定OFF）。enable_reminders=true で基盤を建ててから ON。
      { name = "MORNING_DIGEST_REMINDERS", value = (var.enable_reminders && var.morning_digest_reminders) ? "true" : "false" },
      { name = "REMINDER_LEAD_MINUTES", value = tostring(var.reminder_lead_minutes) },
      { name = "REMINDER_SCHEDULER_GROUP", value = var.enable_reminders ? aws_scheduler_schedule_group.reminders[0].name : "" },
      { name = "REMINDER_QUEUE_ARN", value = var.enable_reminders ? aws_sqs_queue.reminders[0].arn : "" },
      { name = "REMINDER_SCHEDULER_ROLE_ARN", value = var.enable_reminders ? aws_iam_role.reminder_scheduler[0].arn : "" },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
      { name = "GOOGLE_OAUTH_JSON", valueFrom = data.aws_secretsmanager_secret.morning_digest_google_oauth[0].arn },
      # build_user_credentials() は CONNECT_GOOGLE_CLIENT_ID/SECRET(env) を要求する。per-user の
      # refresh token は Slack「連携」＝connect(web 型)クライアントで発行されるため、リフレッシュも
      # 同じ web 型クライアントでないと RefreshError になる（desktop 型の GOOGLE_OAUTH_JSON では不可）。
      # connect-web / fargate と同じ connect_google_client_secret を使う。欠落すると mail/calendar
      # 収集が build_user_credentials で失敗し全 0 件になる（2026-06-25 回帰）。
      { name = "CONNECT_GOOGLE_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_google_client_secret[0].arn },
      # live パリティ（2026-07-11 監査）: ✏️/📅 等メールアクションリンクの HMAC 署名鍵。connect-web 側の
      # 検証鍵と同一 secret（live は database-url の文字列を鍵として共用・rev32 実機と同値）。
      # 剥がれると生成する全アクション URL が検証不能になり、ボタン機能有効化時に沈黙する地雷になる。
      { name = "MAIL_ACTION_HMAC_SECRET", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.morning_digest.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "morning-digest"
      }
    }
    # Scheduled Task なので healthCheck 不要（exit code が成否を語る）
  }])
}

# --- EventBridge → ECS RunTask の IAM role ---
# events.amazonaws.com からの AssumeRole policy（本ファイル独立定義・Phase 2 PR の ingest_schedule.tf
# 側でも `events_assume` を定義する設計なので、merge 時は片方を残して conflict 解消する）。
data "aws_iam_policy_document" "events_morning_digest_assume" {
  count = var.enable_morning_digest ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_morning_digest_invoke" {
  count              = var.enable_morning_digest ? 1 : 0
  name               = "${var.project_name}-${var.environment}-events-morning-digest-invoke"
  assume_role_policy = data.aws_iam_policy_document.events_morning_digest_assume[0].json
}

data "aws_iam_policy_document" "events_morning_digest_run_task" {
  count = var.enable_morning_digest ? 1 : 0
  statement {
    sid       = "RunMorningDigestTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.morning_digest[0].arn, "/:[0-9]+$/", ":*")]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid     = "PassExecutionAndTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.ecs_execution_morning_digest[0].arn,
      aws_iam_role.morning_digest_task[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "events_morning_digest_run_task" {
  count  = var.enable_morning_digest ? 1 : 0
  name   = "${var.project_name}-${var.environment}-events-morning-digest-run-task"
  role   = aws_iam_role.events_morning_digest_invoke[0].id
  policy = data.aws_iam_policy_document.events_morning_digest_run_task[0].json
}

# --- EventBridge rule: 平日 0:30 UTC = 9:30 JST ---
variable "morning_digest_compact" {
  description = "朝ダイジェストの密度優先描画（1件=1行・本文プレビュー廃止・〈他N件〉統一）。2026-07-13 パイロットFB対応。既定 false=旧描画。"
  type        = bool
  default     = false
}

variable "morning_digest_rule_enabled" {
  description = "朝ダイジェストの EventBridge ルールを ENABLED にするか。live は手動 DISABLED 運用のため既定 false（2026-07-11 監査: state 未指定だと provider 既定 ENABLED になり、apply のたびに手動 DISABLE が勝手に巻き戻る）。ロールアウト時はこの変数を true にして apply で点灯する（CLI enable-rule は次回 apply で戻るため使わない）。"
  type        = bool
  default     = false
}

resource "aws_cloudwatch_event_rule" "morning_digest_weekday" {
  count               = var.enable_morning_digest ? 1 : 0
  name                = "${var.project_name}-${var.environment}-morning-digest-weekday"
  description         = "平日朝 9:30 JST の morning_digest Fargate 起動トリガ"
  schedule_expression = var.morning_digest_schedule_expression
  state               = var.morning_digest_rule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "morning_digest_run_task" {
  count    = var.enable_morning_digest && var.mcp_image != "" ? 1 : 0
  rule     = aws_cloudwatch_event_rule.morning_digest_weekday[0].name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events_morning_digest_invoke[0].arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.morning_digest[0].arn
    task_count          = 1
    launch_type         = "FARGATE"
    platform_version    = "LATEST"

    network_configuration {
      subnets          = data.aws_subnets.default.ids
      security_groups  = [aws_security_group.morning_digest[0].id]
      assign_public_ip = true
    }
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 1
  }
}

# ---------- Outputs ----------
output "morning_digest_task_definition_arn" {
  description = "morning_digest Scheduled Task の TaskDefinition ARN（手動 run-task 検証用）"
  value       = var.enable_morning_digest && var.mcp_image != "" ? aws_ecs_task_definition.morning_digest[0].arn : ""
}

output "morning_digest_log_group" {
  description = "CloudWatch Logs グループ"
  value       = aws_cloudwatch_log_group.morning_digest.name
}

output "morning_digest_event_rule" {
  description = "EventBridge rule 名（Test Event で起動検証）"
  value       = var.enable_morning_digest ? aws_cloudwatch_event_rule.morning_digest_weekday[0].name : ""
}

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
      # OAUTH_KMS_KEY_ID は token store の復号に必要（既存 alias を流用）。
      { name = "OAUTH_KMS_KEY_ID", value = "alias/teamagent-oauth-tokens" },
      { name = "OAUTH_KMS_REGION", value = var.aws_region },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
      { name = "GOOGLE_OAUTH_JSON", valueFrom = data.aws_secretsmanager_secret.morning_digest_google_oauth[0].arn },
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
resource "aws_cloudwatch_event_rule" "morning_digest_weekday" {
  count               = var.enable_morning_digest ? 1 : 0
  name                = "${var.project_name}-${var.environment}-morning-digest-weekday"
  description         = "平日朝 9:30 JST の morning_digest Fargate 起動トリガ"
  schedule_expression = var.morning_digest_schedule_expression
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

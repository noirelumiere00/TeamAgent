# ============================================================
# §V: reminder-scan を ECS Scheduled Task で起動（平日日中・「後で」の再通知）
# ============================================================
# 役割: mail_thread_state で status=snoozed かつ snooze_until<=now の行を抽出し、
#   本人 Slack DM にボタン付きカードを再投稿→status を open に戻す（one-shot 再通知）。
#   scripts/run_reminder_scan_fargate.py を teamagent-mcp image で起動する。
#
# enable_interactive_mail ゲート（ボタン機能と一体）。morning_digest_schedule.tf と同パターン。
# 軽量タスク: Bedrock/KMS/Google 不要（RDS 読み書き + 第2 App token で Slack post のみ）。

# ---------- 変数 ----------
variable "fargate_reminder_cpu" {
  description = "reminder-scan タスク CPU（RDS 走査 + Slack post のみ＝軽量）"
  type        = number
  default     = 512
}

variable "fargate_reminder_memory" {
  description = "reminder-scan タスク メモリ MB"
  type        = number
  default     = 1024
}

variable "reminder_schedule_expression" {
  description = "EventBridge cron 式（既定: 平日 0/3/6 UTC = 9/12/15 JST に再通知スキャン）"
  type        = string
  default     = "cron(0 0,3,6 ? * MON-FRI *)"
}

# ---------- CloudWatch Logs（非ゲート・無害） ----------
resource "aws_cloudwatch_log_group" "reminder_scan" {
  name              = "/${var.project_name}/${var.environment}/reminder-scan"
  retention_in_days = 30
}

# ---------- 以降は enable_interactive_mail ゲート ----------

# --- 実行ロール（launch 時 secrets 注入用: DATABASE_URL + 第2 App token のみ） ---
resource "aws_iam_role" "ecs_execution_reminder" {
  count              = var.enable_interactive_mail ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-reminder"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_reminder_managed" {
  count      = var.enable_interactive_mail ? 1 : 0
  role       = aws_iam_role.ecs_execution_reminder[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_reminder_secrets" {
  count = var.enable_interactive_mail ? 1 : 0
  statement {
    sid     = "ReadReminderSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      data.aws_secretsmanager_secret.database_url.arn,
      data.aws_secretsmanager_secret.interactive_mail_bot_token[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_reminder_secrets" {
  count  = var.enable_interactive_mail ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-reminder-secrets"
  role   = aws_iam_role.ecs_execution_reminder[0].id
  policy = data.aws_iam_policy_document.ecs_execution_reminder_secrets[0].json
}

# --- タスクロール: AWS API 直叩きは無い（RDS=DATABASE_URL/SG・Slack=token）＝最小（assume のみ） ---
resource "aws_iam_role" "reminder_task" {
  count              = var.enable_interactive_mail ? 1 : 0
  name               = "${var.project_name}-${var.environment}-reminder-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# --- SG: ingress なし・egress only（Slack/RDS への外向き） ---
resource "aws_security_group" "reminder_scan" {
  count       = var.enable_interactive_mail ? 1 : 0
  name        = "${var.project_name}-${var.environment}-reminder-scan-sg"
  description = "reminder-scan Scheduled Task (egress only)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-reminder-scan-sg" }
}

# RDS への 5432 を reminder SG から許可（純加算）
resource "aws_security_group_rule" "db_from_reminder" {
  count                    = var.enable_interactive_mail ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.reminder_scan[0].id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from reminder-scan Scheduled Task"
}

# --- Task Definition（teamagent-mcp image 流用・command 差分のみ） ---
resource "aws_ecs_task_definition" "reminder_scan" {
  count                    = var.enable_interactive_mail && var.mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-reminder-scan"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_reminder_cpu
  memory                   = var.fargate_reminder_memory
  execution_role_arn       = aws_iam_role.ecs_execution_reminder[0].arn
  task_role_arn            = aws_iam_role.reminder_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "reminder-scan"
    image     = var.mcp_image
    essential = true
    command   = ["python", "scripts/run_reminder_scan_fargate.py"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "STRUCTLOG_FORMAT", value = "json" },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      { name = "INTERACTIVE_MAIL_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.interactive_mail_bot_token[0].arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.reminder_scan.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "reminder-scan"
      }
    }
  }])
}

# --- EventBridge → ECS RunTask ---
data "aws_iam_policy_document" "events_reminder_assume" {
  count = var.enable_interactive_mail ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_reminder_invoke" {
  count              = var.enable_interactive_mail ? 1 : 0
  name               = "${var.project_name}-${var.environment}-events-reminder-invoke"
  assume_role_policy = data.aws_iam_policy_document.events_reminder_assume[0].json
}

data "aws_iam_policy_document" "events_reminder_run_task" {
  count = var.enable_interactive_mail ? 1 : 0
  statement {
    sid       = "RunReminderTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.reminder_scan[0].arn, "/:[0-9]+$/", ":*")]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid     = "PassReminderRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.ecs_execution_reminder[0].arn,
      aws_iam_role.reminder_task[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "events_reminder_run_task" {
  count  = var.enable_interactive_mail ? 1 : 0
  name   = "${var.project_name}-${var.environment}-events-reminder-run-task"
  role   = aws_iam_role.events_reminder_invoke[0].id
  policy = data.aws_iam_policy_document.events_reminder_run_task[0].json
}

resource "aws_cloudwatch_event_rule" "reminder_scan" {
  count               = var.enable_interactive_mail ? 1 : 0
  name                = "${var.project_name}-${var.environment}-reminder-scan"
  description         = "平日日中の「後で」リマインダ再通知スキャン"
  schedule_expression = var.reminder_schedule_expression
}

resource "aws_cloudwatch_event_target" "reminder_run_task" {
  count    = var.enable_interactive_mail && var.mcp_image != "" ? 1 : 0
  rule     = aws_cloudwatch_event_rule.reminder_scan[0].name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events_reminder_invoke[0].arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.reminder_scan[0].arn
    task_count          = 1
    launch_type         = "FARGATE"
    platform_version    = "LATEST"

    network_configuration {
      subnets          = data.aws_subnets.default.ids
      security_groups  = [aws_security_group.reminder_scan[0].id]
      assign_public_ip = true
    }
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 1
  }
}

# ---------- Outputs ----------
output "reminder_scan_task_definition_arn" {
  description = "reminder-scan Scheduled Task の TaskDefinition ARN（手動 run-task 検証用）"
  value       = var.enable_interactive_mail && var.mcp_image != "" ? aws_ecs_task_definition.reminder_scan[0].arn : ""
}

output "reminder_scan_log_group" {
  description = "CloudWatch Logs グループ"
  value       = aws_cloudwatch_log_group.reminder_scan.name
}

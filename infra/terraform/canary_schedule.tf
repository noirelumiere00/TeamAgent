# ============================================================
# 柱3（2026-06-22 事故対策）: 主要経路の合成カナリアを ECS Scheduled Task で 1h ごとに実行
# ============================================================
# 「壊れても自動で気づかない」を潰す。scripts/run_canary_health.py が identity 解決
# （Slack user_id→本人 email・全 per-user 機能の前提となる共有依存）を合成的に叩き、
# 壊れていたら canary_health_result overall=false ＋ exit 非0 を出す → metric filter→alarm→SNS で
# 約1時間以内に自動通知（ユーザー申告を待たない）。image は teamagent-mcp を流用。
# identity 解決は Slack API のみ＝Bedrock/RDS/Google 不要＝最小権限・軽量(256/512)。
# morning_digest_schedule.tf を雛形に簡略化。

# ---------- 変数 ----------
variable "enable_canary_health" {
  description = "カナリア ECS Scheduled Task（taskdef/EventBridge rule/target/IAM）を有効化"
  type        = bool
  default     = false
}

variable "canary_schedule_expression" {
  description = "EventBridge スケジュール式（既定: 1時間ごと）"
  type        = string
  default     = "rate(1 hour)"
}

variable "canary_slack_user_id" {
  description = "カナリアが identity 解決を試す社内 Slack user_id（必ず解決できる人）"
  type        = string
  default     = "U09CX1CCBLN"
}

# ---------- CloudWatch Logs（ゲート外＝常設・enable OFF でも metric filter 先が存在） ----------
resource "aws_cloudwatch_log_group" "canary" {
  name              = "/${var.project_name}/${var.environment}/canary-health"
  retention_in_days = 30
}

# ---------- カナリア失敗 alarm（ゲート外・データが無ければ notBreaching） ----------
resource "aws_cloudwatch_log_metric_filter" "canary_unhealthy" {
  name           = "${var.project_name}-${var.environment}-canary-unhealthy"
  log_group_name = aws_cloudwatch_log_group.canary.name
  pattern        = "{ $.event = \"canary_health_result\" && $.overall IS FALSE }"

  metric_transformation {
    name          = "CanaryUnhealthy"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "canary_unhealthy" {
  alarm_name          = "${var.project_name}-${var.environment}-canary-unhealthy"
  alarm_description   = "AiLa 主要経路の合成カナリアが失敗（identity 解決等が壊れている＝per-user 機能の無音停止）"
  namespace           = local.metric_namespace
  metric_name         = "CanaryUnhealthy"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# ---------- 以降は enable_canary_health ゲート ----------

# 実行ロール（launch 時 secrets 注入・awslogs 書込み） ---
resource "aws_iam_role" "ecs_execution_canary" {
  count              = var.enable_canary_health ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-canary"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_canary_managed" {
  count      = var.enable_canary_health ? 1 : 0
  role       = aws_iam_role.ecs_execution_canary[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_canary_secrets" {
  count = var.enable_canary_health ? 1 : 0
  statement {
    sid       = "ReadCanarySecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.slack_bot.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_canary_secrets" {
  count  = var.enable_canary_health ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-canary-secrets"
  role   = aws_iam_role.ecs_execution_canary[0].id
  policy = data.aws_iam_policy_document.ecs_execution_canary_secrets[0].json
}

# タスクロール: AWS API は叩かない（Slack は token・logs は exec ロール）ため最小（assume のみ）。
resource "aws_iam_role" "canary_task" {
  count              = var.enable_canary_health ? 1 : 0
  name               = "${var.project_name}-${var.environment}-canary-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# SG: ingress なし・egress only（Slack API への外向きのみ） ---
resource "aws_security_group" "canary" {
  count       = var.enable_canary_health ? 1 : 0
  name        = "${var.project_name}-${var.environment}-canary-sg"
  description = "canary Scheduled Task (egress only: Slack API)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-canary-sg" }
}

# Task Definition ---
resource "aws_ecs_task_definition" "canary" {
  count                    = var.enable_canary_health && var.mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-canary"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution_canary[0].arn
  task_role_arn            = aws_iam_role.canary_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "canary"
    image     = var.mcp_image
    essential = true
    command   = ["python", "scripts/run_canary_health.py"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "STRUCTLOG_FORMAT", value = "json" },
      { name = "CANARY_SLACK_USER_ID", value = var.canary_slack_user_id },
    ]
    secrets = [
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.canary.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "canary"
      }
    }
    # Scheduled Task なので healthCheck 不要（exit code が成否を語る）
  }])
}

# EventBridge → ECS RunTask の IAM role ---
data "aws_iam_policy_document" "events_canary_assume" {
  count = var.enable_canary_health ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_canary_invoke" {
  count              = var.enable_canary_health ? 1 : 0
  name               = "${var.project_name}-${var.environment}-events-canary-invoke"
  assume_role_policy = data.aws_iam_policy_document.events_canary_assume[0].json
}

data "aws_iam_policy_document" "events_canary_run_task" {
  count = var.enable_canary_health ? 1 : 0
  statement {
    sid       = "RunCanaryTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.canary[0].arn, "/:[0-9]+$/", ":*")]
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
      aws_iam_role.ecs_execution_canary[0].arn,
      aws_iam_role.canary_task[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "events_canary_run_task" {
  count  = var.enable_canary_health ? 1 : 0
  name   = "${var.project_name}-${var.environment}-events-canary-run-task"
  role   = aws_iam_role.events_canary_invoke[0].id
  policy = data.aws_iam_policy_document.events_canary_run_task[0].json
}

# EventBridge rule: 1時間ごと ---
resource "aws_cloudwatch_event_rule" "canary_hourly" {
  count               = var.enable_canary_health ? 1 : 0
  name                = "${var.project_name}-${var.environment}-canary-hourly"
  description         = "1時間ごとの AiLa 合成カナリア Fargate 起動トリガ"
  schedule_expression = var.canary_schedule_expression
}

resource "aws_cloudwatch_event_target" "canary_run_task" {
  count    = var.enable_canary_health && var.mcp_image != "" ? 1 : 0
  rule     = aws_cloudwatch_event_rule.canary_hourly[0].name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events_canary_invoke[0].arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.canary[0].arn
    task_count          = 1
    launch_type         = "FARGATE"
    platform_version    = "LATEST"

    network_configuration {
      subnets          = data.aws_subnets.default.ids
      security_groups  = [aws_security_group.canary[0].id]
      assign_public_ip = true
    }
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 1
  }
}

# ---------- Outputs ----------
output "canary_task_definition_arn" {
  description = "カナリア Scheduled Task の TaskDefinition ARN（手動 run-task 検証用）"
  value       = var.enable_canary_health && var.mcp_image != "" ? aws_ecs_task_definition.canary[0].arn : ""
}

output "canary_log_group" {
  description = "カナリアの CloudWatch Logs グループ"
  value       = aws_cloudwatch_log_group.canary.name
}

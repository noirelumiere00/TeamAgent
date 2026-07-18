# ============================================================
# 予定リマインド（v0.3 Task 5）— EventBridge Scheduler → SQS FIFO → Lambda → Slack DM
# ============================================================
# フロー: 朝ダイジェスト(runner)が当日予定の「開始 N 分前」ワンタイム schedule を登録
#   （ActionAfterCompletion=DELETE＝自動掃除・冪等名＝再実行安全・payload に PII 無し）
#   → 発火時に SQS へ → Lambda consumer が Slack DM へ chat.postMessage。
# 権限設計（tiktok_acquire.tf の敵対レビュー原則を踏襲）:
#   - アプリ(morning_digest task)へは scheduler:CreateSchedule + 専用ロールの PassRole のみ
#     （sqs:SendMessage も Slack 通知権限も持たせない）
#   - PassRole は「scheduler.amazonaws.com に渡す時のみ」の Condition で絞る
# 全リソースは enable_reminders（既定 false）でゲート（§10 E1-2）。

variable "enable_reminders" {
  description = "予定リマインド基盤（Scheduler group/SQS FIFO/Lambda）を有効化。既定 false。ON 後に morning_digest 側の morning_digest_reminders=true にする。"
  type        = bool
  default     = false
}

variable "morning_digest_reminders" {
  description = "朝ダイジェスト実行時に当日予定のリマインドを登録する（runner 側フラグ）。enable_reminders=true が前提。"
  type        = bool
  default     = false
}

variable "reminder_lead_minutes" {
  description = "予定開始の何分前に通知するか（既定 5 分・指示書どおり）。"
  type        = number
  default     = 5
}

locals {
  rem_enabled = var.enable_reminders ? 1 : 0
  rem_name    = "${var.project_name}-${var.environment}-reminders"
}

# ---------- SQS FIFO（本体 + DLQ） ----------
# FIFO + content dedup: Scheduler のリトライ再送を 5 分窓で収斂（二重通知の主経路を封じる）。
resource "aws_sqs_queue" "reminders_dlq" {
  count                       = local.rem_enabled
  name                        = "${local.rem_name}-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  sqs_managed_sse_enabled     = true
  message_retention_seconds   = 1209600 # 14日（調査猶予）
}

resource "aws_sqs_queue" "reminders" {
  count                       = local.rem_enabled
  name                        = "${local.rem_name}.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  sqs_managed_sse_enabled     = true
  message_retention_seconds   = 3600 # リマインドは鮮度が命＝1時間で破棄
  visibility_timeout_seconds  = 60
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.reminders_dlq[0].arn
    maxReceiveCount     = 3
  })
}

# ---------- Scheduler group + Scheduler→SQS ロール ----------
resource "aws_scheduler_schedule_group" "reminders" {
  count = local.rem_enabled
  name  = local.rem_name
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reminder_scheduler" {
  count              = local.rem_enabled
  name               = "${local.rem_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "reminder_scheduler_policy" {
  count = local.rem_enabled
  statement {
    sid       = "SendToRemindersQueue"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.reminders[0].arn]
  }
}

resource "aws_iam_role_policy" "reminder_scheduler" {
  count  = local.rem_enabled
  name   = "${local.rem_name}-scheduler-sqs"
  role   = aws_iam_role.reminder_scheduler[0].id
  policy = data.aws_iam_policy_document.reminder_scheduler_policy[0].json
}

# ---------- morning_digest task へ付与する最小権限 ----------
# CreateSchedule は本 group 内のみ・PassRole は scheduler サービスへ渡す時のみ。
data "aws_iam_policy_document" "reminder_producer" {
  count = local.rem_enabled
  statement {
    sid     = "CreateOneTimeReminder"
    actions = ["scheduler:CreateSchedule"]
    resources = [
      "arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule/${local.rem_name}/*",
    ]
  }
  statement {
    sid       = "PassSchedulerRoleOnly"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.reminder_scheduler[0].arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "morning_digest_reminders" {
  # morning_digest task role は enable_morning_digest ゲート配下＝両方 true の時のみ付与
  # （enable_reminders 単独 true だと Invalid index で plan が死ぬ・レビュー M1）。
  count  = (var.enable_reminders && var.enable_morning_digest) ? 1 : 0
  name   = "${local.rem_name}-producer"
  role   = aws_iam_role.morning_digest_task[0].id
  policy = data.aws_iam_policy_document.reminder_producer[0].json
}

# ---------- Lambda consumer（SQS → Slack DM） ----------
data "archive_file" "reminder_notify" {
  count       = local.rem_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/reminder_notify"
  output_path = "${path.module}/build/reminder_notify.zip"
  # __pycache__ を除外（ローカルで handler.py を実行した worktree だと .pyc が混入し、
  # zip が 1950B→5291B に膨らんで source_code_hash が恒常ドリフトする・#194 planでも混入した）。
  excludes = ["__pycache__", "**/__pycache__/**"]
}

resource "aws_iam_role" "reminder_notify" {
  count              = local.rem_enabled
  name               = "${local.rem_name}-notify"
  assume_role_policy = data.aws_iam_policy_document.tiktok_dispatch_assume.json
}

data "aws_iam_policy_document" "reminder_notify_policy" {
  count = local.rem_enabled
  statement {
    sid       = "SqsConsume"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.reminders[0].arn]
  }
  statement {
    sid       = "ReadSlackBotToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.slack_bot.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "reminder_notify" {
  count  = local.rem_enabled
  name   = "${local.rem_name}-notify"
  role   = aws_iam_role.reminder_notify[0].id
  policy = data.aws_iam_policy_document.reminder_notify_policy[0].json
}

resource "aws_lambda_function" "reminder_notify" {
  count            = local.rem_enabled
  function_name    = "${local.rem_name}-notify"
  role             = aws_iam_role.reminder_notify[0].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  timeout          = 30
  filename         = data.archive_file.reminder_notify[0].output_path
  source_code_hash = data.archive_file.reminder_notify[0].output_base64sha256
  environment {
    variables = {
      SLACK_BOT_TOKEN_SECRET_NAME = var.slack_bot_token_secret_name
    }
  }
}

resource "aws_lambda_event_source_mapping" "reminder_notify" {
  count            = local.rem_enabled
  function_name    = aws_lambda_function.reminder_notify[0].arn
  event_source_arn = aws_sqs_queue.reminders[0].arn
  batch_size       = 1 # 部分失敗の複雑さを持たない（失敗 raise → リトライ → DLQ）
}

# ---------- DLQ 滞留 → ops 通知（v0.3 §2.5 の DLQ→ops 自動通知） ----------
resource "aws_cloudwatch_metric_alarm" "reminders_dlq" {
  count               = local.rem_enabled
  alarm_name          = "${local.rem_name}-dlq-depth"
  alarm_description   = "予定リマインドの DLQ に滞留あり＝通知が届いていない（runbook: docs/v3.2/ops 参照）"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    QueueName = aws_sqs_queue.reminders_dlq[0].name
  }
  # SQS はアイドル~6時間でメトリクス発行が止まる＝missing を正常扱い（他アラームと同流儀）。
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]
}

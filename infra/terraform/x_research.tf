# ============================================================
# x-research — X(Twitter)リサーチ（カタログ①②④）＋SaaSコスト台帳
# ============================================================
# ①②(同期): MCP コンテナ内から Apify REST を叩くだけ＝追加インフラ不要。
# ④(非同期・A′トポロジ): OC(AiLa)→MCPツール(SQS SendMessageのみ)
#   →Lambda dispatcher(RunTask/PassRole保有)→使い捨てFargate(軽量Python・mcp image流用)
#   →S3(x-research/) / DynamoDB(状態)。tiktok_acquire.tf の複製・軽量版。
# MCPロールには RunTask/PassRole を絶対に持たせない(権限はLambdaに集約)。
# 全リソースは var.enable_x_research でgate(既定OFF・後方互換)。
# Apify トークンは既存 teamagent/<env>/tiktok/apify-token を共用(新設しない・計画裁定)。
# ------------------------------------------------------------

variable "enable_x_research" {
  description = "x-research 一式(SQS/Lambda/DynamoDB/taskdef/IAM)と MCP の X系envを作成する。"
  type        = bool
  default     = false
}

variable "x_analysis_model_id" {
  description = "X系の分析(ニーズ分類/山分析)用 Bedrock モデルID。未指定は mcp 既定(Haiku)に落ちる。"
  type        = string
  default     = "jp.anthropic.claude-sonnet-4-6"
}

variable "cost_apify_monthly_usd" {
  description = "Apify の月次全体予算(USD)。超過は cost_guard が fail-close で実行拒否。"
  type        = string
  default     = "50"
}

variable "pr_research_allowed_emails" {
  description = "カタログ系スキルの段階公開 allowlist(カンマ区切りemail・空=全員許可)。stage1=小俣のみ。"
  type        = string
  default     = ""
}

locals {
  xr_enabled = var.enable_x_research ? 1 : 0
  # ワーカーは mcp image 流用(command上書き)のため、taskdef は mcp_image が要る。
  xr_task_on  = (var.enable_x_research && var.mcp_image != "") ? 1 : 0
  xr_name     = "${var.project_name}-${var.environment}-x-buzz"
  xr_acct     = data.aws_caller_identity.current.account_id
  xr_loggroup = "/teamagent/${var.environment}/x-buzz"
}

# ---------- CloudWatch Logs ----------
resource "aws_cloudwatch_log_group" "x_buzz" {
  count             = local.xr_enabled
  name              = local.xr_loggroup
  retention_in_days = 30
}

# ---------- SQS(jobs) + DLQ ----------
resource "aws_sqs_queue" "x_jobs_dlq" {
  count                     = local.xr_enabled
  name                      = "${local.xr_name}-dlq"
  message_retention_seconds = 1209600 # 14日
}

resource "aws_sqs_queue" "x_jobs" {
  count                      = local.xr_enabled
  name                       = "${local.xr_name}-jobs"
  visibility_timeout_seconds = 1800
  message_retention_seconds  = 86400
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.x_jobs_dlq[0].arn
    maxReceiveCount     = 3
  })
}

# ---------- DynamoDB(jobs 状態) ----------
resource "aws_dynamodb_table" "x_jobs" {
  count        = local.xr_enabled
  name         = "${local.xr_name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"
  attribute {
    name = "job_id"
    type = "S"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ---------- DynamoDB(SaaSコスト台帳・プロバイダ横断) ----------
# adapters/cost_guard.py の月次$台帳。Apify/xAI 等の外部SaaS課金を月次JSTで原子加算する。
# 既存防御(budgets.tf=AWS課金 / quota_store=動画本数 / max_budget_usd=Bedrock実行)との
# 役割分担は cost_guard.py docstring 参照。第一義の利用者が x-research のためここに置くが、
# テーブル自体はプロバイダ横断の共有インフラ（第二弾 proposal_job も同じ台帳に記帳する）。
resource "aws_dynamodb_table" "cost_usage" {
  count        = local.xr_enabled
  name         = "${var.project_name}-${var.environment}-cost-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "usage_key"
  attribute {
    name = "usage_key"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# ---------- Security Group(egress-only) ----------
resource "aws_security_group" "x_buzz_tasks" {
  count       = local.xr_enabled
  name        = "${local.xr_name}-sg"
  description = "x-buzz worker Fargate tasks (egress only)"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- IAM: 実行ロール(ECRプル/Secrets/ログ) ----------
resource "aws_iam_role" "x_buzz_exec" {
  count              = local.xr_enabled
  name               = "${local.xr_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "x_buzz_exec_managed" {
  count      = local.xr_enabled
  role       = aws_iam_role.x_buzz_exec[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Apify トークンは tiktok/ 名前空間の既存 secret を共用（teamagent/<env>/tiktok/apify-token）。
data "aws_iam_policy_document" "x_buzz_exec_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.aws_region}:${local.xr_acct}:secret:${var.project_name}/${var.environment}/tiktok/*"]
  }
}

resource "aws_iam_role_policy" "x_buzz_exec_secrets" {
  count  = local.xr_enabled
  name   = "${local.xr_name}-exec-secrets"
  role   = aws_iam_role.x_buzz_exec[0].id
  policy = data.aws_iam_policy_document.x_buzz_exec_secrets.json
}

# ---------- IAM: タスクロール(S3 put / Dynamo更新 / コスト台帳 / ログ) ----------
resource "aws_iam_role" "x_buzz_task" {
  count              = local.xr_enabled
  name               = "${local.xr_name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "x_buzz_task_app" {
  statement {
    sid       = "S3PutPrefix"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw_files.arn}/x-research/*"]
  }
  statement {
    sid       = "DynamoStatus"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.x_jobs[0].arn]
  }
  statement {
    sid       = "CostLedger"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.cost_usage[0].arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.x_buzz[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "x_buzz_task_app" {
  count  = local.xr_enabled
  name   = "${local.xr_name}-task-app"
  role   = aws_iam_role.x_buzz_task[0].id
  policy = data.aws_iam_policy_document.x_buzz_task_app.json
}

# ---------- ECS Task Definition(軽量・mcp image流用・chromium不要) ----------
# tiktok_acquire と違い httpx→Apify だけなので 0.5vCPU/1GB で足りる。
# image は teamagent-mcp を流用し command 上書き（morning_digest_schedule.tf と同方式）。
resource "aws_ecs_task_definition" "x_buzz_worker" {
  count                    = local.xr_task_on
  family                   = "${local.xr_name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.x_buzz_exec[0].arn
  task_role_arn            = aws_iam_role.x_buzz_task[0].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "runtime-tmp"
  }

  container_definitions = jsonencode([
    merge(local.teamagent_runtime_container, {
      name      = "worker"
      image     = var.mcp_image
      essential = true
      command   = [local.teamagent_python, "-m", "teamagent.workers.x_buzz_job"]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "X_S3_BUCKET", value = aws_s3_bucket.raw_files.bucket },
        { name = "X_JOBS_TABLE", value = aws_dynamodb_table.x_jobs[0].name },
        { name = "COST_GUARD_TABLE", value = aws_dynamodb_table.cost_usage[0].name },
        { name = "COST_APIFY_MONTHLY_USD", value = var.cost_apify_monthly_usd },
      ]
      secrets = var.tiktok_apify_secret_arn != "" ? [
        { name = "APIFY_API_TOKEN", valueFrom = var.tiktok_apify_secret_arn }
      ] : []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = local.xr_loggroup
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    })
  ])
}

# ---------- SQS → Lambda dispatcher → ECS RunTask (★RunTask/PassRoleはここだけ) ----------
data "archive_file" "x_dispatch" {
  count       = local.xr_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/x_dispatch"
  output_path = "${path.module}/build/x_dispatch.zip"
}

resource "aws_iam_role" "x_dispatch" {
  count              = local.xr_enabled
  name               = "${local.xr_name}-dispatch"
  assume_role_policy = data.aws_iam_policy_document.tiktok_dispatch_assume.json
}

data "aws_iam_policy_document" "x_dispatch_policy" {
  statement {
    sid       = "SqsConsume"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.x_jobs[0].arn]
  }
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.aws_region}:${local.xr_acct}:task-definition/${local.xr_name}-worker:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid       = "PassRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.x_buzz_exec[0].arn, aws_iam_role.x_buzz_task[0].arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${local.xr_acct}:*"]
  }
}

resource "aws_iam_role_policy" "x_dispatch_policy" {
  count  = local.xr_enabled
  name   = "${local.xr_name}-dispatch-policy"
  role   = aws_iam_role.x_dispatch[0].id
  policy = data.aws_iam_policy_document.x_dispatch_policy.json
}

resource "aws_lambda_function" "x_dispatch" {
  count            = local.xr_task_on
  function_name    = "${local.xr_name}-dispatch"
  role             = aws_iam_role.x_dispatch[0].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.x_dispatch[0].output_path
  source_code_hash = data.archive_file.x_dispatch[0].output_base64sha256
  timeout          = 30
  environment {
    variables = {
      CLUSTER_ARN = aws_ecs_cluster.main.arn
      TASKDEF_ARN = aws_ecs_task_definition.x_buzz_worker[0].arn
      SUBNETS     = join(",", data.aws_subnets.default.ids)
      SG_ID       = aws_security_group.x_buzz_tasks[0].id
      CONTAINER   = "worker"
    }
  }
}

resource "aws_lambda_event_source_mapping" "x_dispatch" {
  count            = local.xr_task_on
  event_source_arn = aws_sqs_queue.x_jobs[0].arn
  function_name    = aws_lambda_function.x_dispatch[0].arn
  batch_size       = 1
}

# ---------- IAM: MCPタスクロールに付ける権限 ----------
# submit(SQS/Dynamo put) / status(Dynamo get+update=report_urlキャッシュ) / 結果読込(S3 get)
# / コスト台帳(check/record)。★RunTask/PassRole は絶対に含めない(tiktok と同じ権限分離)。
data "aws_iam_policy_document" "x_mcp_policy" {
  statement {
    sid       = "SqsSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.x_jobs[0].arn]
  }
  statement {
    sid       = "DynamoJobs"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.x_jobs[0].arn]
  }
  statement {
    sid       = "CostLedger"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.cost_usage[0].arn]
  }
  statement {
    sid       = "S3ReadResults"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.raw_files.arn}/x-research/*"]
  }
}

resource "aws_iam_role_policy" "x_mcp_policy" {
  count  = local.xr_enabled
  name   = "${local.xr_name}-mcp-access"
  role   = aws_iam_role.mcp_task.id
  policy = data.aws_iam_policy_document.x_mcp_policy.json
}

# ---------- CloudWatch: DLQ 深度アラーム（既存SNSへ） ----------
resource "aws_cloudwatch_metric_alarm" "x_jobs_dlq_depth" {
  count               = local.xr_enabled
  alarm_name          = "${local.xr_name}-dlq-depth"
  alarm_description   = "x-buzz ジョブがDLQに落ちている(3回失敗)。ワーカー/Apify障害を確認。"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions = {
    QueueName = aws_sqs_queue.x_jobs_dlq[0].name
  }
  alarm_actions = [aws_sns_topic.alarms.arn]
}

# ---------- S3 ライフサイクル ----------
# x-research/ の expire ルールは lambda_iam.tf の統合 lifecycle_configuration に追加済み。
# 同一バケットに lifecycle リソースを2つ置くと全ルール置換で交互上書きになる既知地雷のため
# ここには置かない（tiktok_acquire.tf 2026-07-11 と同じ扱い）。

# ---------- 出力 ----------
output "x_jobs_queue_url" {
  value       = local.xr_enabled == 1 ? aws_sqs_queue.x_jobs[0].url : null
  description = "MCP(x_buzz_measure)が SendMessage する先。env X_TASK_QUEUE に設定。"
}
output "x_jobs_table_name" {
  value       = local.xr_enabled == 1 ? aws_dynamodb_table.x_jobs[0].name : null
  description = "MCP(x_buzz_measure_status)が読む先。env X_JOBS_TABLE に設定。"
}
output "cost_usage_table_name" {
  value       = local.xr_enabled == 1 ? aws_dynamodb_table.cost_usage[0].name : null
  description = "SaaSコスト台帳。env COST_GUARD_TABLE に設定。"
}

# ============================================================
# tiktok-acquire — AI駆動TikTok取得サービス (A′トポロジ)
# ============================================================
# OC(AiLa)→MCPツール(SQS SendMessageのみ)→EventBridge Pipe(RunTask/PassRole保有)
#   →使い捨てFargate(chromium+yt-dlp+ffmpeg)→S3(成果) / DynamoDB(状態)
# MCPロールには RunTask/PassRole を絶対に持たせない(権限はPipeに集約)。
# 全リソースは var.enable_tiktok_acquire でgate(既定OFF・後方互換)。
# 本番ONの前提: ToS/stealthの法務承認(O1) ＋ env-gate USE_TIKTOK_ACQUIRE=1。
# ------------------------------------------------------------

variable "enable_tiktok_acquire" {
  description = "tiktok-acquire 一式(ECS/SQS/Pipe/DynamoDB/IAM)を作成する。法務承認後に true。"
  type        = bool
  default     = false
}

variable "tiktok_acquire_image" {
  description = "tiktok-acquireのECR完全digest URI。例 718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-dev-tiktok-acquire@sha256:<64hex>。tagは禁止。"
  type        = string
  default     = ""

  validation {
    condition = var.tiktok_acquire_image == "" || can(regex(
      "^[0-9]+\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$",
      var.tiktok_acquire_image,
    ))
    error_message = "tiktok_acquire_imageはECRの完全digest URI（...@sha256:<64hex>）で指定してください。tagは禁止です。"
  }
}

variable "tiktok_task_cpu" {
  description = "Fargate vCPU(1024=1vCPU)。chromium+ffmpeg向けに2048推奨。"
  type        = string
  default     = "2048"
}

variable "tiktok_task_memory" {
  description = "Fargate メモリ(MiB)。4096〜8192。"
  type        = string
  default     = "4096"
}

variable "tiktok_ephemeral_gib" {
  description = "一時ストレージ(GiB)。動画数百MB向けに30〜50。"
  type        = number
  default     = 40

  validation {
    condition = (
      var.tiktok_ephemeral_gib >= 21 &&
      var.tiktok_ephemeral_gib <= 200 &&
      floor(var.tiktok_ephemeral_gib) == var.tiktok_ephemeral_gib
    )
    error_message = "tiktok_ephemeral_gibはFargateで指定可能な21〜200GiBの整数にしてください。"
  }
}

variable "tiktok_proxy_secret_arn" {
  description = "プロキシ資格情報のSecrets Manager ARN(任意)。空なら直結(WAFリスク上昇)。"
  type        = string
  default     = ""
}

variable "tiktok_apify_secret_arn" {
  description = "Apifyトークンの Secrets Manager ARN(任意・会社管理キー)。"
  type        = string
  default     = ""
}

variable "tiktok_mcp_task_role_name" {
  description = "OC/AiLa(MCP)が走るタスクロール名。SQS送信/Dynamo参照/S3署名の権限を付与する対象。空ならスキップ(手動付与)。"
  type        = string
  default     = ""
}

locals {
  tk_enabled  = var.enable_tiktok_acquire ? 1 : 0
  tk_name     = "${var.project_name}-${var.environment}-tiktok-acquire"
  tk_acct     = data.aws_caller_identity.current.account_id
  tk_loggroup = "/teamagent/${var.environment}/tiktok-acquire"
  # コンテナに渡す secrets(ARNが与えられた時だけ)
  tk_secrets = concat(
    var.tiktok_proxy_secret_arn != "" ? [{ name = "PROXY_SERVER", valueFrom = var.tiktok_proxy_secret_arn }] : [],
    var.tiktok_apify_secret_arn != "" ? [{ name = "APIFY_API_TOKEN", valueFrom = var.tiktok_apify_secret_arn }] : [],
  )
  tk_dispatch_static_environment = {
    CLUSTER_ARN = aws_ecs_cluster.tiktok[0].arn
    SUBNETS     = join(",", data.aws_subnets.default.ids)
    SG_ID       = aws_security_group.tiktok_tasks[0].id
    CONTAINER   = "acquire"
  }
}

# Keep count-gated policy document address migrations explicit so targeted
# read-only runtime plans include the prior state instances deterministically.
moved {
  from = data.aws_iam_policy_document.tiktok_task_app
  to   = data.aws_iam_policy_document.tiktok_task_app[0]
}

moved {
  from = data.aws_iam_policy_document.tiktok_dispatch_policy
  to   = data.aws_iam_policy_document.tiktok_dispatch_policy[0]
}

moved {
  from = data.aws_iam_policy_document.tiktok_mcp_policy
  to   = data.aws_iam_policy_document.tiktok_mcp_policy[0]
}

# ---------- ECR ----------
resource "aws_ecr_repository" "tiktok_acquire" {
  count                = local.tk_enabled
  name                 = local.tk_name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

# ---------- ECS Cluster ----------
resource "aws_ecs_cluster" "tiktok" {
  count = local.tk_enabled
  name  = "${var.project_name}-${var.environment}-tiktok"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------- CloudWatch Logs ----------
resource "aws_cloudwatch_log_group" "tiktok_acquire" {
  count             = local.tk_enabled
  name              = local.tk_loggroup
  retention_in_days = 30
}

# ---------- SQS(jobs) + DLQ ----------
resource "aws_sqs_queue" "tiktok_jobs_dlq" {
  count                     = local.tk_enabled
  name                      = "${local.tk_name}-dlq"
  message_retention_seconds = 1209600 # 14日
}

resource "aws_sqs_queue" "tiktok_jobs" {
  count                      = local.tk_enabled
  name                       = "${local.tk_name}-jobs"
  visibility_timeout_seconds = 1800 # ジョブ最長(分単位)に合わせる
  message_retention_seconds  = 86400
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tiktok_jobs_dlq[0].arn
    maxReceiveCount     = 3
  })
}

# ---------- DynamoDB(jobs 状態) ----------
resource "aws_dynamodb_table" "tiktok_jobs" {
  count        = local.tk_enabled
  name         = "${local.tk_name}-jobs"
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

# ---------- Security Group(egress-only) ----------
resource "aws_security_group" "tiktok_tasks" {
  count       = local.tk_enabled
  name        = "${local.tk_name}-sg"
  description = "tiktok-acquire Fargate tasks (egress only)"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- IAM: 実行ロール(ECRプル/Secrets/ログ) ----------
data "aws_iam_policy_document" "tiktok_exec_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tiktok_exec" {
  count              = local.tk_enabled
  name               = "${local.tk_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.tiktok_exec_assume.json
}

resource "aws_iam_role_policy_attachment" "tiktok_exec_managed" {
  count      = local.tk_enabled
  role       = aws_iam_role.tiktok_exec[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# 実行ロールが Secrets を注入できるように(tiktok配下のみ)
data "aws_iam_policy_document" "tiktok_exec_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.aws_region}:${local.tk_acct}:secret:${var.project_name}/${var.environment}/tiktok/*"]
  }
}

resource "aws_iam_role_policy" "tiktok_exec_secrets" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-exec-secrets"
  role   = aws_iam_role.tiktok_exec[0].id
  policy = data.aws_iam_policy_document.tiktok_exec_secrets.json
}

# ---------- IAM: タスクロール(S3 prefix put / Dynamo更新) ----------
data "aws_iam_policy_document" "tiktok_task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tiktok_task" {
  count              = local.tk_enabled
  name               = "${local.tk_name}-task"
  assume_role_policy = data.aws_iam_policy_document.tiktok_task_assume.json
}

data "aws_iam_policy_document" "tiktok_task_app" {
  count = local.tk_enabled

  statement {
    sid       = "S3PutPrefix"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw_files.arn}/tiktok-acquire/*"]
  }
  statement {
    sid       = "DynamoStatus"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
}

resource "aws_iam_role_policy" "tiktok_task_app" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-task-app"
  role   = aws_iam_role.tiktok_task[0].id
  policy = data.aws_iam_policy_document.tiktok_task_app[0].json
}

# ---------- ECS Task Definition(arm64) ----------
resource "aws_ecs_task_definition" "tiktok_acquire" {
  count                    = local.tk_enabled
  family                   = local.tk_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.tiktok_task_cpu
  memory                   = var.tiktok_task_memory
  execution_role_arn       = aws_iam_role.tiktok_exec[0].arn
  task_role_arn            = aws_iam_role.tiktok_task[0].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  ephemeral_storage {
    size_in_gib = var.tiktok_ephemeral_gib
  }

  # chromium/yt-dlp/ffmpegは一時ファイルを必要とする。readonly rootを維持し、
  # Fargate ephemeral storageの/tmpだけを書込み可能にする。
  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "acquire"
      image                  = var.tiktok_acquire_image
      essential              = true
      user                   = "10001"
      readonlyRootFilesystem = true
      linuxParameters = {
        initProcessEnabled = true
        capabilities = {
          drop = ["ALL"]
        }
      }
      # command はイメージのCMD(npx tsx src/job.ts)を使用。Pipeが env TIKTOK_JOB_JSON を上書き注入。
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "TIKTOK_S3_BUCKET", value = aws_s3_bucket.raw_files.bucket },
        { name = "TIKTOK_JOBS_TABLE", value = aws_dynamodb_table.tiktok_jobs[0].name },
        { name = "TMPDIR", value = "/tmp" },
        { name = "HOME", value = "/tmp" },
        { name = "XDG_CACHE_HOME", value = "/tmp/.cache" },
      ]
      secrets = local.tk_secrets
      mountPoints = [
        { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = local.tk_loggroup
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "acquire"
        }
      }
    }
  ])

  lifecycle {
    create_before_destroy = true

    precondition {
      condition = can(regex(
        "^${local.tk_acct}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${local.tk_name}@sha256:[0-9a-f]{64}$",
        var.tiktok_acquire_image,
      ))
      error_message = "enable_tiktok_acquire=trueでは同一account/regionの専用ECR完全digest URIが必須です。"
    }

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

# ---------- SQS → Lambda dispatcher → ECS RunTask (★RunTask/PassRoleはここだけ) ----------
# EventBridge Pipes のECS動的override注入は壊れやすいため、前例(lambda_iam.tf)準拠の
# 薄いLambdaでSQSをデキューし ecs.run_task(containerOverrides=TIKTOK_JOB_JSON) を呼ぶ。
data "archive_file" "tiktok_dispatch" {
  count       = local.tk_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/tiktok_dispatch"
  output_path = "${path.module}/build/tiktok_dispatch.zip"
  # reminder_notifyと同様、.pyc混入によるsource_code_hashドリフトを防止する。
  excludes = ["__pycache__", "**/__pycache__/**"]
}

data "aws_iam_policy_document" "tiktok_dispatch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tiktok_dispatch" {
  count              = local.tk_enabled
  name               = "${local.tk_name}-dispatch"
  assume_role_policy = data.aws_iam_policy_document.tiktok_dispatch_assume.json
}

data "aws_iam_policy_document" "tiktok_dispatch_policy" {
  count = local.tk_enabled

  statement {
    sid       = "SqsConsume"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.aws_region}:${local.tk_acct}:task-definition/${local.tk_name}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.tiktok[0].arn]
    }
  }
  statement {
    sid       = "PassRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.tiktok_exec[0].arn, aws_iam_role.tiktok_task[0].arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${local.tk_acct}:*"]
  }
}

resource "aws_iam_role_policy" "tiktok_dispatch_policy" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-dispatch-policy"
  role   = aws_iam_role.tiktok_dispatch[0].id
  policy = data.aws_iam_policy_document.tiktok_dispatch_policy[0].json
}

resource "aws_lambda_function" "tiktok_dispatch" {
  count            = local.tk_enabled
  function_name    = "${local.tk_name}-dispatch"
  role             = aws_iam_role.tiktok_dispatch[0].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.tiktok_dispatch[0].output_path
  source_code_hash = data.archive_file.tiktok_dispatch[0].output_base64sha256
  timeout          = 30
  environment {
    variables = merge(local.tk_dispatch_static_environment, {
      TASKDEF_ARN = aws_ecs_task_definition.tiktok_acquire[0].arn
    })
  }

  lifecycle {
    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

resource "aws_lambda_event_source_mapping" "tiktok_dispatch" {
  count            = local.tk_enabled
  event_source_arn = aws_sqs_queue.tiktok_jobs[0].arn
  function_name    = aws_lambda_function.tiktok_dispatch[0].arn
  batch_size       = 1

  lifecycle {
    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

# ---------- IAM: OC/AiLa(MCP)ロールに付ける権限(SQS送信/Dynamo参照/S3署名) ----------
# ★RunTask/PassRoleは絶対に含めない(権限分離=敵対レビューhigh対応)
data "aws_iam_policy_document" "tiktok_mcp_policy" {
  count = local.tk_enabled

  statement {
    sid       = "SqsSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.tiktok_jobs[0].arn]
  }
  statement {
    # submit() creates the initial job record with PutItem; status polling uses
    # GetItem. UpdateItem remains confined to the acquire worker task role.
    sid       = "DynamoStatusSubmit"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "S3GetForPresign"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.raw_files.arn}/tiktok-acquire/*"]
  }
}

resource "aws_iam_role_policy" "tiktok_mcp_policy" {
  # MCPロール名が与えられた時だけ付与(空なら手動付与)
  count  = (local.tk_enabled == 1 && var.tiktok_mcp_task_role_name != "") ? 1 : 0
  name   = "${local.tk_name}-mcp-access"
  role   = var.tiktok_mcp_task_role_name
  policy = data.aws_iam_policy_document.tiktok_mcp_policy[0].json
}

# ---------- S3 ライフサイクル(tiktok-acquire/ を30日でexpire) ----------
# tiktok-acquire-expire ルールは lambda_iam.tf の aws_s3_bucket_lifecycle_configuration.raw_files
# に統合（2026-07-11）。同一バケットに lifecycle_configuration リソースを2つ置くと
# PutBucketLifecycleConfiguration が全ルール置換のため交互上書きになる（2026-07-06 の既知地雷）。
# ここには再追加しないこと。

# ---------- 出力 ----------
output "tiktok_jobs_queue_url" {
  value       = local.tk_enabled == 1 ? aws_sqs_queue.tiktok_jobs[0].url : null
  description = "OC(MCP submit)が SendMessage する先。env TIKTOK_TASK_QUEUE に設定。"
}
output "tiktok_jobs_table_name" {
  value       = local.tk_enabled == 1 ? aws_dynamodb_table.tiktok_jobs[0].name : null
  description = "OC(MCP status)が GetItem する先。env TIKTOK_JOBS_TABLE に設定。"
}
output "tiktok_acquire_ecr_url" {
  value       = local.tk_enabled == 1 ? aws_ecr_repository.tiktok_acquire[0].repository_url : null
  description = "ここに Dockerfile.acquire を build/push(immutable tag)。"
}

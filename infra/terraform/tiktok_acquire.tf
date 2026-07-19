# ============================================================
# generic media jobs — one-shot isolated media worker (A′ topology)
# ============================================================
# Core→SQS (SendMessage only)→Lambda dispatcher (RunTask/PassRole)
#   →one-shot Fargate media image→dedicated S3/DynamoDB. ECS overrideには
#   envelope本体を載せず、DynamoDB上のjob ID/payload digest pointerだけを渡す。
# Legacy ``enable_tiktok_acquire`` and image input remain aliases so an existing
# deployment can migrate without silently disabling the TikTok route.
# ------------------------------------------------------------

variable "enable_tiktok_acquire" {
  description = "Deprecated compatibility alias for enable_media_worker."
  type        = bool
  default     = false
}

variable "tiktok_acquire_image" {
  description = "Deprecated compatibility alias for media_worker_image."
  type        = string
  default     = ""
  validation {
    condition = var.tiktok_acquire_image == "" || can(regex(
      "^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$",
      var.tiktok_acquire_image,
    ))
    error_message = "tiktok_acquire_image must be empty or an immutable ECR image digest URI."
  }
}

variable "enable_media_worker" {
  description = "Create the generic isolated media queue/task/table/bucket/dispatcher/janitor."
  type        = bool
  default     = false
}

variable "media_worker_image" {
  description = "Immutable ARM64 teamagent-media-worker image URI (digest reference required by promotion tooling)."
  type        = string
  default     = ""
  validation {
    condition = var.media_worker_image == "" || can(regex(
      "^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$",
      var.media_worker_image,
    ))
    error_message = "media_worker_image must be empty or an immutable ECR image digest URI."
  }
}

variable "media_artifact_ttl_seconds" {
  description = "Maximum normal retention before the deterministic janitor deletes a terminal job prefix."
  type        = number
  default     = 3600
  validation {
    condition     = var.media_artifact_ttl_seconds >= 300 && var.media_artifact_ttl_seconds <= 21600
    error_message = "media_artifact_ttl_seconds must be between 300 and 21600."
  }
}

variable "tiktok_task_cpu" {
  description = "Fargate vCPU(1024=1vCPU)。chromium+ffmpeg向けに2048推奨。"
  type        = string
  default     = "2048"
}

variable "tiktok_task_memory" {
  description = "Fargate メモリ(MiB)。runtime contract は4096固定。"
  type        = string
  default     = "4096"
  validation {
    condition     = var.tiktok_task_memory == "4096"
    error_message = "tiktok_task_memory must remain 4096 MiB."
  }
}

variable "tiktok_ephemeral_gib" {
  description = "一時ストレージ(GiB)。動画数百MB向けに30〜50。"
  type        = number
  default     = 40
}

variable "tiktok_proxy_secret_arn" {
  description = "Deprecated external browser proxy. Must remain empty because Chromium is forced through the in-container DNS-pinned proxy."
  type        = string
  default     = ""
  validation {
    condition     = var.tiktok_proxy_secret_arn == ""
    error_message = "tiktok_proxy_secret_arn is disabled; Chromium must use the in-container DNS-pinned proxy."
  }
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
  media_worker_enabled = var.enable_media_worker || var.enable_tiktok_acquire
  media_enabled        = local.media_worker_enabled ? 1 : 0
  # Keep existing physical names/state addresses while widening their contract.
  # Promotion tooling exposes MEDIA_* names; no destructive resource rename is
  # required merely to adopt the generic envelope.
  media_name         = "${var.project_name}-${var.environment}-tiktok-acquire"
  media_worker_image = var.media_worker_image != "" ? var.media_worker_image : var.tiktok_acquire_image
  media_bucket_name  = "${var.project_name}-${var.environment}-media-jobs-${data.aws_caller_identity.current.account_id}"
  tk_enabled         = local.media_enabled
  tk_name            = local.media_name
  tk_acct            = data.aws_caller_identity.current.account_id
  tk_loggroup        = "/teamagent/${var.environment}/tiktok-acquire"
  # コンテナに渡す secrets(ARNが与えられた時だけ)
  tk_secrets = var.tiktok_apify_secret_arn != "" ? [
    { name = "APIFY_API_TOKEN", valueFrom = var.tiktok_apify_secret_arn }
  ] : []
}

# ---------- ECR ----------
resource "aws_ecr_repository" "tiktok_acquire" {
  count                = local.tk_enabled
  name                 = local.tk_name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
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
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "tiktok_jobs" {
  count                      = local.tk_enabled
  name                       = "${local.tk_name}-jobs"
  visibility_timeout_seconds = 1800 # ジョブ最長(分単位)に合わせる
  message_retention_seconds  = 86400
  sqs_managed_sse_enabled    = true
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
  point_in_time_recovery {
    enabled = true
  }
}

# ---------- Dedicated S3 artifact bucket ----------
# ``Expires`` metadata and DynamoDB TTL are not deletion mechanisms.  The
# scheduled janitor below is authoritative; this one-day rule is only a
# provider-side backstop for objects staged before a row can be created.
resource "aws_s3_bucket" "media_jobs" {
  count  = local.tk_enabled
  bucket = local.media_bucket_name
}

resource "aws_s3_bucket_public_access_block" "media_jobs" {
  count                   = local.tk_enabled
  bucket                  = aws_s3_bucket.media_jobs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media_jobs" {
  count  = local.tk_enabled
  bucket = aws_s3_bucket.media_jobs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media_jobs" {
  count  = local.tk_enabled
  bucket = aws_s3_bucket.media_jobs[0].id

  rule {
    id     = "media-jobs-emergency-backstop"
    status = "Enabled"
    filter {
      prefix = "media-jobs/"
    }
    expiration {
      days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_server_side_encryption_configuration.media_jobs]
}

data "aws_iam_policy_document" "media_jobs_bucket" {
  count = local.tk_enabled
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.media_jobs[0].arn,
      "${aws_s3_bucket.media_jobs[0].arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "media_jobs" {
  count  = local.tk_enabled
  bucket = aws_s3_bucket.media_jobs[0].id
  policy = data.aws_iam_policy_document.media_jobs_bucket[0].json
}

# ---------- Security Group(egress-only) ----------
resource "aws_security_group" "tiktok_tasks" {
  count       = local.tk_enabled
  name        = "${local.tk_name}-sg"
  description = "tiktok-acquire Fargate tasks (egress only)"
  vpc_id      = data.aws_vpc.default.id
  egress {
    description = "HTTPS only for allowlisted public media sites and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "VPC resolver UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["${cidrhost(data.aws_vpc.default.cidr_block, 2)}/32"]
  }
  egress {
    description = "VPC resolver TCP fallback"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["${cidrhost(data.aws_vpc.default.cidr_block, 2)}/32"]
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

# ---------- IAM: タスクロール(S3 prefix put / Dynamo更新 / ログ) ----------
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
    sid = "S3ObjectsWithinMediaJobs"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*"]
  }
  statement {
    sid       = "S3ListOnlyMediaJobs"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media_jobs[0].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["media-jobs/*"]
    }
  }
  statement {
    sid       = "DynamoStatus"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.tiktok_acquire[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "tiktok_task_app" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-task-app"
  role   = aws_iam_role.tiktok_task[0].id
  policy = data.aws_iam_policy_document.tiktok_task_app[0].json
}

# ---------- Generic ECS media worker Task Definition (arm64) ----------
moved {
  from = aws_ecs_task_definition.tiktok_acquire
  to   = aws_ecs_task_definition.media_worker
}

resource "aws_ecs_task_definition" "media_worker" {
  count                    = local.tk_enabled
  family                   = "${local.media_name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.tiktok_task_cpu
  memory                   = var.tiktok_task_memory
  execution_role_arn       = aws_iam_role.tiktok_exec[0].arn
  task_role_arn            = aws_iam_role.tiktok_task[0].arn
  depends_on               = [terraform_data.production_image_release_gate]

  lifecycle {
    precondition {
      condition = local.media_worker_image != "" && can(regex(
        "^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$",
        local.media_worker_image,
      ))
      error_message = "media_worker_image must be an immutable image digest reference."
    }
  }

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  ephemeral_storage {
    size_in_gib = var.tiktok_ephemeral_gib
  }

  volume {
    name = "runtime-tmp"
  }

  container_definitions = jsonencode([
    merge(local.teamagent_runtime_container, {
      name        = "media-worker"
      image       = local.media_worker_image
      essential   = true
      stopTimeout = 30
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "MEDIA_JOB_BUCKET", value = aws_s3_bucket.media_jobs[0].bucket },
        { name = "MEDIA_JOBS_TABLE", value = aws_dynamodb_table.tiktok_jobs[0].name },
        { name = "MEDIA_ARTIFACT_TTL_SECONDS", value = tostring(var.media_artifact_ttl_seconds) },
        { name = "MEDIA_BLOCKED_VPC_CIDRS", value = data.aws_vpc.default.cidr_block },
      ]
      secrets = local.tk_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = local.tk_loggroup
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "media-worker"
        }
      }
    })
  ])
}

# ---------- SQS → Lambda dispatcher → ECS RunTask (★RunTask/PassRoleはここだけ) ----------
# EventBridge Pipes のECS動的override注入は壊れやすいため、前例(lambda_iam.tf)準拠の
# 薄いLambdaでSQSをデキューし、strict canonical envelopeを検証してから
# ecs.run_taskには8192文字制限内のjob ID/payload digest pointerだけを渡す。
data "archive_file" "tiktok_dispatch" {
  count       = local.tk_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/tiktok_dispatch"
  output_path = "${path.module}/build/tiktok_dispatch.zip"
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
    resources = ["arn:aws:ecs:${var.aws_region}:${local.tk_acct}:task-definition/${local.media_name}-worker:*"]
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
    sid       = "MarkDispatchFailure"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
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
    variables = {
      CLUSTER_ARN                = aws_ecs_cluster.tiktok[0].arn
      TASKDEF_ARN                = aws_ecs_task_definition.media_worker[0].arn
      JOBS_TABLE                 = aws_dynamodb_table.tiktok_jobs[0].name
      JOB_BUCKET                 = aws_s3_bucket.media_jobs[0].bucket
      MEDIA_ARTIFACT_TTL_SECONDS = tostring(var.media_artifact_ttl_seconds)
      SUBNETS                    = join(",", data.aws_subnets.default.ids)
      SG_ID                      = aws_security_group.tiktok_tasks[0].id
      CONTAINER                  = "media-worker"
    }
  }
}

resource "aws_lambda_event_source_mapping" "tiktok_dispatch" {
  count            = local.tk_enabled
  event_source_arn = aws_sqs_queue.tiktok_jobs[0].arn
  function_name    = aws_lambda_function.tiktok_dispatch[0].arn
  batch_size       = 1
}

# ---------- Deterministic cleanup janitor (5 minute cadence) ----------
data "archive_file" "media_janitor" {
  count       = local.tk_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/media_janitor"
  output_path = "${path.module}/build/media_janitor.zip"
}

resource "aws_iam_role" "media_janitor" {
  count              = local.tk_enabled
  name               = "${local.media_name}-janitor"
  assume_role_policy = data.aws_iam_policy_document.tiktok_dispatch_assume.json
}

data "aws_iam_policy_document" "media_janitor" {
  count = local.tk_enabled
  statement {
    sid       = "DynamoFencedCleanup"
    actions   = ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "S3ListMediaJobs"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media_jobs[0].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["media-jobs/*"]
    }
  }
  statement {
    sid       = "S3DeleteMediaJobs"
    actions   = ["s3:DeleteObject", "s3:GetObject", "s3:GetObjectTagging"]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${local.tk_acct}:*"]
  }
}

resource "aws_iam_role_policy" "media_janitor" {
  count  = local.tk_enabled
  name   = "${local.media_name}-janitor"
  role   = aws_iam_role.media_janitor[0].id
  policy = data.aws_iam_policy_document.media_janitor[0].json
}

resource "aws_lambda_function" "media_janitor" {
  count            = local.tk_enabled
  function_name    = "${local.media_name}-janitor"
  role             = aws_iam_role.media_janitor[0].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.media_janitor[0].output_path
  source_code_hash = data.archive_file.media_janitor[0].output_base64sha256
  timeout          = 240
  environment {
    variables = {
      JOBS_TABLE = aws_dynamodb_table.tiktok_jobs[0].name
      JOB_BUCKET = aws_s3_bucket.media_jobs[0].bucket
    }
  }
}

resource "aws_cloudwatch_event_rule" "media_janitor" {
  count               = local.tk_enabled
  name                = "${local.media_name}-janitor"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "media_janitor" {
  count     = local.tk_enabled
  rule      = aws_cloudwatch_event_rule.media_janitor[0].name
  target_id = "media-janitor"
  arn       = aws_lambda_function.media_janitor[0].arn
}

resource "aws_lambda_permission" "media_janitor" {
  count         = local.tk_enabled
  statement_id  = "AllowEventBridgeMediaJanitor"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.media_janitor[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.media_janitor[0].arn
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
    sid       = "DynamoJobs"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "S3JobObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*"]
  }
}

resource "aws_iam_role_policy" "tiktok_mcp_policy" {
  # The core task that receives MEDIA_* must atomically receive the matching
  # queue/table/bucket permissions; no manual role-name side channel.
  count  = local.tk_enabled
  name   = "${local.tk_name}-mcp-access"
  role   = aws_iam_role.mcp_task.name
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
  description = "Deprecated alias for the generic media-worker ECR URL."
}
output "media_jobs_bucket_name" {
  value       = local.tk_enabled == 1 ? aws_s3_bucket.media_jobs[0].bucket : null
  description = "Dedicated encrypted media artifact bucket."
}
output "media_jobs_queue_url" {
  value       = local.tk_enabled == 1 ? aws_sqs_queue.tiktok_jobs[0].url : null
  description = "Generic media submit queue URL."
}
output "media_jobs_table_name" {
  value       = local.tk_enabled == 1 ? aws_dynamodb_table.tiktok_jobs[0].name : null
  description = "Generic media job state table."
}
output "media_worker_task_definition_arn" {
  value       = local.tk_enabled == 1 ? aws_ecs_task_definition.media_worker[0].arn : null
  description = "Generic one-shot media worker task definition."
}

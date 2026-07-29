# ============================================================
# generic media jobs — one-shot isolated media worker (A′ topology)
# ============================================================
# Core→SQS (SendMessage only)→trusted Lambda dispatcher/finalizer
#   →task-roleなしone-shot Fargate media tool→presigned S3 capabilities。
# ECS overrideにはidentity hashとprivate S3 .env ARNだけを載せ、AWS資格情報・
# DynamoDB・bucket権限はtool containerへ一切渡さない。
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
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-media-worker@sha256:[0-9a-f]{64}$",
      var.tiktok_acquire_image,
    ))
    error_message = "tiktok_acquire_image must be the TeamAgent media-worker release digest in the fixed dev account and Tokyo region."
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
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-media-worker@sha256:[0-9a-f]{64}$",
      var.media_worker_image,
    ))
    error_message = "media_worker_image must be the TeamAgent media-worker release digest in the fixed dev account and Tokyo region."
  }
}

variable "media_artifact_ttl_seconds" {
  description = "Thirty-day media artifact retention before deterministic janitor cleanup."
  type        = number
  default     = 2592000
  validation {
    condition     = var.media_artifact_ttl_seconds == 2592000
    error_message = "media_artifact_ttl_seconds must remain exactly 2592000 (30 days)."
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
  description = "Deprecated external browser proxy. Must remain empty because Chromium is forced through the in-container DNS-pinned proxy."
  type        = string
  default     = ""
  validation {
    condition     = var.tiktok_proxy_secret_arn == ""
    error_message = "tiktok_proxy_secret_arn is disabled; Chromium must use the in-container DNS-pinned proxy."
  }
}

variable "tiktok_apify_secret_arn" {
  description = "Deprecated unused Apify token. Must remain empty for the generic media worker."
  type        = string
  default     = ""

  validation {
    condition     = var.tiktok_apify_secret_arn == ""
    error_message = "tiktok_apify_secret_arn is unused and must remain empty."
  }
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
  media_name = "${var.project_name}-${var.environment}-tiktok-acquire"
  # Keep both persisted/caller-controlled inputs generic-only. Before the
  # cutover, only the exact live=desired sync attested by runtime_guard_live may
  # recover the deployed legacy digest; after cutover this fallback is empty.
  media_worker_image = (
    var.media_worker_image != "" ? var.media_worker_image :
    var.tiktok_acquire_image != "" ? var.tiktok_acquire_image :
    local.pre_media_cutover_sync_image
  )
  media_bucket_name = "${var.project_name}-${var.environment}-media-jobs-${data.aws_caller_identity.current.account_id}"
  tk_enabled        = local.media_enabled
  tk_name           = local.media_name
  tk_acct           = data.aws_caller_identity.current.account_id
  tk_loggroup       = "/teamagent/${var.environment}/tiktok-acquire"
  tk_dispatch_static_environment = {
    # Keep the always-present runtime guard independent from the guarded ECS
    # task revision while binding every non-revision dispatcher input exactly.
    CLUSTER_ARN                = "arn:aws:ecs:${var.aws_region}:${local.tk_acct}:cluster/${var.project_name}-${var.environment}-tiktok"
    JOBS_TABLE                 = aws_dynamodb_table.tiktok_jobs[0].name
    JOB_BUCKET                 = aws_s3_bucket.media_jobs[0].bucket
    MEDIA_ARTIFACT_TTL_SECONDS = tostring(var.media_artifact_ttl_seconds)
    SUBNETS                    = join(",", data.aws_subnets.default.ids)
    SG_ID                      = aws_security_group.tiktok_tasks[0].id
    CONTAINER                  = "acquire"
  }
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

  lifecycle {
    prevent_destroy = true
  }
}

# ---------- CloudWatch Logs ----------
resource "aws_cloudwatch_log_group" "tiktok_acquire" {
  count             = local.tk_enabled
  name              = local.tk_loggroup
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "tiktok_dispatch" {
  name              = "/aws/lambda/${local.tk_name}-dispatch"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.tiktok_dispatch
  id = "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch"
}

# ---------- SQS(jobs) + DLQ ----------
resource "aws_sqs_queue" "tiktok_jobs_dlq" {
  count                     = local.tk_enabled
  name                      = "${local.tk_name}-dlq"
  message_retention_seconds = 1209600 # 14日
  sqs_managed_sse_enabled   = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "tiktok_jobs" {
  count = local.tk_enabled
  name  = "${local.tk_name}-jobs"
  # Lambda retries must occur before the immutable 900-second job deadline.
  # 180 seconds is also the AWS-required 6x multiple of the 30-second handler timeout.
  visibility_timeout_seconds = 180
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tiktok_jobs_dlq[0].arn
    maxReceiveCount     = 5
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "media_stopped_delivery_dlq" {
  count                     = local.tk_enabled
  name                      = "${local.media_name}-stopped-delivery-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "media_stopped_invocation_dlq" {
  count                     = local.tk_enabled
  name                      = "${local.media_name}-stopped-invocation-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  lifecycle {
    prevent_destroy = true
  }
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

  lifecycle {
    prevent_destroy = true
  }
}

# ---------- Dedicated S3 artifact bucket ----------
# ``Expires`` metadata and DynamoDB TTL are not deletion mechanisms.  The
# scheduled janitor below is authoritative; this 30-day rule is only a
# provider-side backstop for objects staged before a row can be created.
resource "aws_s3_bucket" "media_jobs" {
  count  = local.tk_enabled
  bucket = local.media_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "media_jobs" {
  count  = local.tk_enabled
  bucket = aws_s3_bucket.media_jobs[0].id

  versioning_configuration {
    status = "Enabled"
  }
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
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [
    aws_s3_bucket_server_side_encryption_configuration.media_jobs,
    aws_s3_bucket_versioning.media_jobs,
  ]
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
  statement {
    sid     = "DenyStaleMediaUploadCapabilities"
    effect  = "Deny"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/attempts/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "NumericGreaterThan"
      variable = "s3:signatureAge"
      values   = ["900000"]
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

  lifecycle {
    prevent_destroy = true
  }
}

# ---------- IAM: 実行ロール(ECR pull/logs + immutable control env only) ----------
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

data "aws_iam_policy_document" "tiktok_exec_control" {
  count = local.tk_enabled
  statement {
    sid       = "LocateDispatcherControlBucket"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.media_jobs[0].arn]
  }
  statement {
    sid       = "ReadDispatcherControlEnvironment"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/control/*.env"]
  }
}

resource "aws_iam_role_policy" "tiktok_exec_control" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-exec-control"
  role   = aws_iam_role.tiktok_exec[0].id
  policy = data.aws_iam_policy_document.tiktok_exec_control[0].json
}

# ---------- Roleless generic ECS media tool Task Definition (arm64) ----------
# Keep the existing Terraform address, family, and physical container name so
# the generic worker is an in-place, reviewable runtime migration.
resource "aws_ecs_task_definition" "tiktok_acquire" {
  count                    = local.tk_enabled
  family                   = local.tk_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.tiktok_task_cpu
  memory                   = var.tiktok_task_memory
  execution_role_arn       = aws_iam_role.tiktok_exec[0].arn
  skip_destroy             = true

  depends_on = [
    terraform_data.runtime_guard,
    terraform_data.production_image_release_gate,
  ]

  lifecycle {
    create_before_destroy = true

    precondition {
      condition = (
        can(regex(
          "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-media-worker@sha256:[0-9a-f]{64}$",
          local.media_worker_image,
        )) ||
        (
          var.media_worker_image == "" &&
          var.tiktok_acquire_image == "" &&
          local.pre_media_cutover_sync &&
          local.media_worker_image == local.pre_media_cutover_sync_image
        )
      )
      error_message = "media_worker_image must be the fixed TeamAgent media-worker release digest, except for the exact runtime-guard-bound pre-cutover legacy sync."
    }

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
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
      name        = "acquire"
      image       = local.media_worker_image
      essential   = true
      stopTimeout = 30
      environment = [
        { name = "HOME", value = "/tmp/home" },
        { name = "TMPDIR", value = "/tmp" },
        { name = "XDG_CACHE_HOME", value = "/tmp/.cache" },
        { name = "PYTHONPYCACHEPREFIX", value = "/tmp/.pycache" },
        { name = "MEDIA_BLOCKED_VPC_CIDRS", value = data.aws_vpc.default.cidr_block },
      ]
      secrets = []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = local.tk_loggroup
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "acquire"
        }
      }
    })
  ])
}

# ---------- SQS → trusted dispatcher/finalizer → roleless ECS tool ----------
# EventBridge Pipes のECS動的override注入は壊れやすいため、前例(lambda_iam.tf)準拠の
# Lambdaでstrict canonical envelopeを検証し、VersionId固定GETとslot固定POSTを発行。
# toolはS3/Dynamo権限を持たず、STOPPED後にLambdaがchecksumとattempt fenceを確定する。
data "archive_file" "tiktok_dispatch" {
  count            = local.tk_enabled
  type             = "zip"
  source_file      = "${path.module}/lambda/tiktok_dispatch/handler.py"
  output_path      = "${path.module}/build/tiktok_dispatch.zip"
  output_file_mode = "0644"
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
    resources = ["arn:aws:ecs:${var.aws_region}:${local.tk_acct}:task-definition/${local.media_name}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.tiktok[0].arn]
    }
  }
  statement {
    sid       = "TagStartedTask"
    actions   = ["ecs:TagResource"]
    resources = ["arn:aws:ecs:${var.aws_region}:${local.tk_acct}:task/${aws_ecs_cluster.tiktok[0].name}/*"]
    condition {
      test     = "StringEquals"
      variable = "ecs:CreateAction"
      values   = ["RunTask"]
    }
  }
  statement {
    sid       = "PassRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.tiktok_exec[0].arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid = "IssueAndVerifyExactMediaCapabilities"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/input/*",
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/control/*",
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/attempts/*",
    ]
  }
  statement {
    sid       = "ListExactMediaVersions"
    actions   = ["s3:ListBucketVersions"]
    resources = [aws_s3_bucket.media_jobs[0].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["media-jobs/*"]
    }
  }
  statement {
    sid       = "OwnAuthoritativeMediaLedger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "WriteAsyncFailureDestination"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.media_stopped_invocation_dlq[0].arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.tiktok_dispatch.arn}:*"]
  }
}

resource "aws_iam_role_policy" "tiktok_dispatch_policy" {
  count  = local.tk_enabled
  name   = "${local.tk_name}-dispatch-policy"
  role   = aws_iam_role.tiktok_dispatch[0].id
  policy = data.aws_iam_policy_document.tiktok_dispatch_policy[0].json
}

resource "aws_lambda_function" "tiktok_dispatch" {
  count                          = local.tk_enabled
  function_name                  = "${local.tk_name}-dispatch"
  role                           = aws_iam_role.tiktok_dispatch[0].arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "handler.handler"
  filename                       = data.archive_file.tiktok_dispatch[0].output_path
  source_code_hash               = data.archive_file.tiktok_dispatch[0].output_base64sha256
  timeout                        = 30
  memory_size                    = 512
  reserved_concurrent_executions = 2

  depends_on = [
    aws_cloudwatch_log_group.tiktok_dispatch,
    terraform_data.runtime_guard,
  ]

  environment {
    variables = merge(local.tk_dispatch_static_environment, {
      TASKDEF_ARN = aws_ecs_task_definition.tiktok_acquire[0].arn
    })
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "tiktok_dispatch" {
  count                        = local.tk_enabled
  function_name                = aws_lambda_function.tiktok_dispatch[0].function_name
  maximum_event_age_in_seconds = 21600
  maximum_retry_attempts       = 2

  destination_config {
    on_failure {
      destination = aws_sqs_queue.media_stopped_invocation_dlq[0].arn
    }
  }

  depends_on = [aws_iam_role_policy.tiktok_dispatch_policy]
}

resource "aws_lambda_event_source_mapping" "tiktok_dispatch" {
  count                   = local.tk_enabled
  event_source_arn        = aws_sqs_queue.tiktok_jobs[0].arn
  function_name           = aws_lambda_function.tiktok_dispatch[0].arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 2
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}

# ECS can stop before the worker reaches its fenced terminal write (for
# example, image pull failure, Fargate placement failure, OOM, or SIGKILL).
# Reuse the dispatcher Lambda as a task-family-scoped terminal reconciler.
resource "aws_cloudwatch_event_rule" "media_task_stopped" {
  count = local.tk_enabled
  name  = "${local.media_name}-stopped"
  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.tiktok[0].arn]
      lastStatus = ["STOPPED"]
      taskDefinitionArn = [{
        prefix = "arn:aws:ecs:${var.aws_region}:${local.tk_acct}:task-definition/${local.media_name}:"
      }]
    }
  })

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "media_stopped_delivery_dlq" {
  count = local.tk_enabled

  statement {
    sid     = "AllowEventBridgeStoppedDelivery"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      aws_sqs_queue.media_stopped_delivery_dlq[0].arn,
    ]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.media_task_stopped[0].arn]
    }
  }
}

resource "aws_sqs_queue_policy" "media_stopped_delivery_dlq" {
  count     = local.tk_enabled
  queue_url = aws_sqs_queue.media_stopped_delivery_dlq[0].url
  policy    = data.aws_iam_policy_document.media_stopped_delivery_dlq[0].json
}

resource "aws_cloudwatch_event_target" "media_task_stopped" {
  count     = local.tk_enabled
  rule      = aws_cloudwatch_event_rule.media_task_stopped[0].name
  target_id = "media-task-stopped-reconciler"
  arn       = aws_lambda_function.tiktok_dispatch[0].arn

  dead_letter_config {
    arn = aws_sqs_queue.media_stopped_delivery_dlq[0].arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }

  depends_on = [aws_sqs_queue_policy.media_stopped_delivery_dlq]
}

resource "aws_lambda_permission" "media_task_stopped" {
  count         = local.tk_enabled
  statement_id  = "AllowEventBridgeMediaTaskStopped"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tiktok_dispatch[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.media_task_stopped[0].arn
}

# ---------- CloudWatch: dispatcher DLQ depth ----------
resource "aws_cloudwatch_metric_alarm" "tiktok_jobs_dlq_depth" {
  count               = local.tk_enabled
  alarm_name          = "${local.tk_name}-dlq-depth"
  alarm_description   = "Media job is retained in the DLQ and requires operator review"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    QueueName = aws_sqs_queue.tiktok_jobs_dlq[0].name
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "media_stopped_delivery_dlq_depth" {
  count               = local.tk_enabled
  alarm_name          = "${local.media_name}-stopped-delivery-dlq-depth"
  alarm_description   = "ECS STOPPED EventBridge delivery exhausted retries"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    QueueName = aws_sqs_queue.media_stopped_delivery_dlq[0].name
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "media_stopped_invocation_dlq_depth" {
  count               = local.tk_enabled
  alarm_name          = "${local.media_name}-stopped-invocation-dlq-depth"
  alarm_description   = "ECS STOPPED reconciler exhausted Lambda async retries"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    QueueName = aws_sqs_queue.media_stopped_invocation_dlq[0].name
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ---------- Deterministic cleanup janitor (5 minute cadence) ----------
data "archive_file" "media_janitor" {
  count       = local.tk_enabled
  type        = "zip"
  source_dir  = "${path.module}/lambda/media_janitor"
  output_path = "${path.module}/build/media_janitor.zip"
}

resource "aws_cloudwatch_log_group" "media_janitor" {
  count             = local.tk_enabled
  name              = "/aws/lambda/${local.media_name}-janitor"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
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
    actions   = ["s3:ListBucketVersions"]
    resources = [aws_s3_bucket.media_jobs[0].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["media-jobs/*"]
    }
  }
  statement {
    sid = "S3DeleteMediaJobs"
    actions = [
      "s3:DeleteObjectVersion",
      "s3:GetObjectVersion",
      "s3:GetObjectVersionTagging",
    ]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.media_janitor[0].arn}:*"]
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

  depends_on = [
    aws_cloudwatch_log_group.media_janitor,
    terraform_data.runtime_guard,
  ]

  environment {
    variables = {
      JOBS_TABLE = aws_dynamodb_table.tiktok_jobs[0].name
      JOB_BUCKET = aws_s3_bucket.media_jobs[0].bucket
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_event_rule" "media_janitor" {
  count               = local.tk_enabled
  name                = "${local.media_name}-janitor"
  schedule_expression = "rate(5 minutes)"

  lifecycle {
    prevent_destroy = true
  }
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

# ---------- IAM: OC/AiLa(MCP)ロールに付ける最小権限 ----------
# ★RunTask/PassRoleは絶対に含めない(権限分離=敵対レビューhigh対応)
data "aws_iam_policy_document" "tiktok_mcp_policy" {
  count = local.tk_enabled
  statement {
    sid       = "SqsSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.tiktok_jobs[0].arn]
  }
  statement {
    sid       = "ReadMediaJobStatus"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.tiktok_jobs[0].arn]
  }
  statement {
    sid = "ReadMediaInputsAndFinalArtifacts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/input/*",
      "${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/attempts/*/*/output/*",
    ]
  }
  statement {
    sid = "S3JobInputsWrite"
    actions = [
      "s3:PutObject",
      "s3:PutObjectTagging",
    ]
    resources = ["${aws_s3_bucket.media_jobs[0].arn}/media-jobs/*/input/*"]
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
  description = "DEPRECATED legacy teamagent-dev-tiktok-acquire repository. It is not a runtime image or release push target; use media_worker_ecr_url."
}
output "media_worker_ecr_url" {
  value       = aws_ecr_repository.mcp_media.repository_url
  description = "Canonical signed teamagent-media-worker release repository used by the unified media runtime."
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
  value       = local.tk_enabled == 1 ? aws_ecs_task_definition.tiktok_acquire[0].arn : null
  description = "Generic one-shot media worker task definition."
}

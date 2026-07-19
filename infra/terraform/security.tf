# ============================================================
# セキュリティ基盤 — Sprint 2 / 2.7
# ============================================================
# 構成：
#   - CloudTrail multi-region + log file validation
#   - IAM Access Analyzer（アカウント単位）
#   - Bedrock invocation logging（S3 + KMS）
#   - KMS CMK（CloudTrail / Bedrock 共通利用）
# ============================================================

data "aws_caller_identity" "current" {}

# ---------- KMS CMK（CloudTrail / Bedrock logs 共通暗号化）----------
resource "aws_kms_key" "logs" {
  description             = "TeamAgent ${var.environment} — CloudTrail / Bedrock invocation logs encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  # CloudTrail と Bedrock のサービスプリンシパルから使えるようにする
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRoot"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudTrail"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action = [
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      },
      {
        Sid    = "AllowBedrockLogs"
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action   = "kms:GenerateDataKey"
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.project_name}-${var.environment}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

# ---------- CloudTrail multi-region + log file validation ----------
resource "aws_s3_bucket" "cloudtrail" {
  count         = var.enable_cloudtrail ? 1 : 0
  bucket        = "${var.project_name}-${var.environment}-cloudtrail-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count                   = var.enable_cloudtrail ? 1 : 0
  bucket                  = aws_s3_bucket.cloudtrail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.logs.arn
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail[0].arn
        Condition = {
          StringEquals = {
            "aws:SourceArn" = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${var.project_name}-${var.environment}-trail"
          }
        }
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"  = "bucket-owner-full-control"
            "aws:SourceArn" = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${var.project_name}-${var.environment}-trail"
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.cloudtrail[0].arn,
          "${aws_s3_bucket.cloudtrail[0].arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail && var.enable_cloudtrail_log_delivery ? 1 : 0

  name                          = "${var.project_name}-${var.environment}-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail[0].id
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  kms_key_id                    = aws_kms_key.logs.arn

  event_selector {
    read_write_type           = "All"
    include_management_events = true
    # 提案 PDF を含む S3 オブジェクトの読書を記録（PII リーク監査）
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${aws_s3_bucket.raw_files.arn}/"]
    }
  }

  # First-time enablement is performed before this saved plan by the composed
  # guard: every writer is disconnected, the guard records the exact AWS
  # PutBucketVersioning response Date/request-id, proves a 900-second no-write
  # window twice, rechecks it, and cuts both producers over under one shared
  # lock. A later Enabled observation cannot satisfy this precondition.
  depends_on = [
    aws_s3_bucket_policy.cloudtrail,
    aws_s3_bucket_versioning.cloudtrail,
  ]

  lifecycle {
    precondition {
      condition     = local.log_producer_cutover_guard_valid
      error_message = "CloudTrail writer requires sync parity or a guard-verified producer-off pre-cutover receipt bound to the exact cutover."
    }

    # Pausing or retiring audit delivery requires a separate approved change.
    prevent_destroy = true
  }
}

# ---------- IAM Access Analyzer ----------
resource "aws_accessanalyzer_analyzer" "account" {
  count = var.enable_iam_access_analyzer ? 1 : 0

  analyzer_name = "${var.project_name}-${var.environment}-account-analyzer"
  type          = "ACCOUNT"
}

# ---------- Bedrock invocation logging（S3 + KMS）----------
# 注：bedrock:PutModelInvocationLoggingConfiguration はリージョン×アカウントで1設定のみ。
# 既存設定はguard-owned cutover receiptとstate ownershipを確認してadoptし、同じfull
# saved plan以外のdirect/targeted経路では変更しない。
resource "aws_s3_bucket" "bedrock_logs" {
  count         = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket        = "${var.project_name}-${var.environment}-bedrock-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = false

  lifecycle {
    precondition {
      condition     = var.bedrock_logs_retention_days == 60
      error_message = "Bedrock AI入出力ログの保持期間は承認済みの60日である必要があります。"
    }
  }
}

resource "aws_s3_bucket_versioning" "bedrock_logs" {
  count  = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket = aws_s3_bucket.bedrock_logs[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "bedrock_logs" {
  count                   = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket                  = aws_s3_bucket.bedrock_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bedrock_logs" {
  count  = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket = aws_s3_bucket.bedrock_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.logs.arn
    }
  }
}

# Bedrock invocation payloads are retained only below bedrock/. Current objects
# expire at day 60. An overwrite can make a version noncurrent at any age, so
# noncurrent expiry is also 60 days; no version can be permanently deleted less
# than 60 days after it was created. The conservative overwrite case may retain
# a version longer than 60 days, never shorter.
# A second rule removes expired delete markers that no longer protect any
# object version. No lifecycle is attached to the CloudTrail bucket: audit
# logs remain without automatic deletion.
resource "aws_s3_bucket_lifecycle_configuration" "bedrock_logs" {
  count  = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket = aws_s3_bucket.bedrock_logs[0].id

  rule {
    id     = "bedrock-current-and-noncurrent-minimum-60-days"
    status = "Enabled"

    filter {
      prefix = "bedrock/"
    }

    expiration {
      days = var.bedrock_logs_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.bedrock_logs_retention_days
    }
  }

  rule {
    id     = "bedrock-expired-delete-markers"
    status = "Enabled"

    filter {
      prefix = "bedrock/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.bedrock_logs]
}

# Bedrock が S3 に書き込むためのバケットポリシー
resource "aws_s3_bucket_policy" "bedrock_logs" {
  count  = var.enable_bedrock_invocation_logging ? 1 : 0
  bucket = aws_s3_bucket.bedrock_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowBedrockPut"
        Effect    = "Allow"
        Principal = { Service = "bedrock.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.bedrock_logs[0].arn}/bedrock/AWSLogs/${data.aws_caller_identity.current.account_id}/BedrockModelInvocationLogs/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.bedrock_logs[0].arn,
          "${aws_s3_bucket.bedrock_logs[0].arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid       = "DenyManualBedrockPayloadDeletion"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
        ]
        Resource = "${aws_s3_bucket.bedrock_logs[0].arn}/bedrock/*"
      },
      {
        Sid       = "DenyNonBedrockPayloadWriters"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.bedrock_logs[0].arn}/bedrock/*"
        Condition = {
          StringNotEquals = {
            "aws:PrincipalServiceName" = "bedrock.amazonaws.com"
          }
        }
      },
    ]
  })
}

# Bedrock invocation logging 設定本体
# 注：terraform-provider-aws では aws_bedrock_model_invocation_logging_configuration を利用
resource "aws_bedrock_model_invocation_logging_configuration" "main" {
  count = var.enable_bedrock_invocation_logging && var.enable_bedrock_invocation_log_delivery ? 1 : 0

  logging_config {
    embedding_data_delivery_enabled = true
    image_data_delivery_enabled     = false
    text_data_delivery_enabled      = true
    video_data_delivery_enabled     = false

    s3_config {
      bucket_name = aws_s3_bucket.bedrock_logs[0].id
      key_prefix  = "bedrock/"
    }
  }

  depends_on = [
    aws_s3_bucket_lifecycle_configuration.bedrock_logs,
    aws_s3_bucket_policy.bedrock_logs,
    aws_s3_bucket_versioning.bedrock_logs,
  ]

  lifecycle {
    precondition {
      condition     = local.log_producer_cutover_guard_valid
      error_message = "Bedrock log writer requires sync parity or a guard-verified producer-off pre-cutover receipt bound to the exact cutover."
    }

    # The bootstrap flag must never destroy a live account-level logging
    # configuration. Retirement needs an explicit, separately reviewed path.
    prevent_destroy = true
  }
}

# ---------- Secrets Manager rotation ポリシードキュメント参照 ----------
# 実際の自動ローテーション Lambda は Sprint 14 で実装（docs/v3.2/ops/secrets_rotation_policy.md）。
# ここでは「ローテーションが必要であることを宣言だけする」レベル：
#   - 既存 secret に対するローテーション設定は terraform import が必要なので別 PR で扱う
#   - 当面は手動 90 日サイクル + tag で次回期日を管理
resource "aws_secretsmanager_secret" "db_password_rotation_marker" {
  # 既存 secret には触れず、ローテーション期日を「タグだけで」管理するマーカー secret
  name                    = "${var.project_name}/${var.environment}/_rotation_marker"
  description             = "ローテーション期日トラッキング用（実 secret は別管理）"
  recovery_window_in_days = 0

  tags = {
    NextRotationDue = "2026-08-22" # 90 日サイクル / Sprint 14 で Lambda 自動化
    PolicyDoc       = "docs/v3.2/ops/secrets_rotation_policy.md"
  }
}

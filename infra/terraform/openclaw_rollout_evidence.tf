# ============================================================
# OpenClaw post-apply rollout result and rollback authorization
# ============================================================
# The canonical runtime guard writes only attempt-scoped rollout result and
# signature objects here. The bucket and keys are deliberately separate from
# build/release evidence so the Terraform automation role never gains write or
# signing authority over source, image, or release authorization evidence.

locals {
  openclaw_rollout_evidence_bucket = (
    "${var.project_name}-${var.environment}-openclaw-rollout-evidence"
  )
  openclaw_rollout_evidence_alias = (
    "alias/${var.project_name}-${var.environment}-openclaw-rollout-evidence"
  )
  openclaw_rollout_signing_alias = (
    "alias/${var.project_name}-${var.environment}-openclaw-rollout-signing"
  )
}

resource "aws_kms_key" "openclaw_rollout_evidence" {
  description             = "Encrypt immutable OpenClaw post-apply rollout results"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "openclaw_rollout_evidence" {
  name          = local.openclaw_rollout_evidence_alias
  target_key_id = aws_kms_key.openclaw_rollout_evidence.key_id
}

resource "aws_kms_key" "openclaw_rollout_signing" {
  description              = "Sign immutable OpenClaw post-apply rollout results"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "openclaw_rollout_signing" {
  name          = local.openclaw_rollout_signing_alias
  target_key_id = aws_kms_key.openclaw_rollout_signing.key_id
}

resource "aws_s3_bucket" "openclaw_rollout_evidence" {
  bucket              = local.openclaw_rollout_evidence_bucket
  force_destroy       = false
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "openclaw_rollout_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "openclaw_rollout_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "openclaw_rollout_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.openclaw_rollout_evidence.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "openclaw_rollout_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id

  depends_on = [aws_s3_bucket_versioning.openclaw_rollout_evidence]

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 3650
    }
  }
}

data "aws_iam_policy_document" "openclaw_rollout_evidence_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.openclaw_rollout_evidence.arn,
      "${aws_s3_bucket.openclaw_rollout_evidence.arn}/*",
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
    sid       = "DenyUnencryptedRolloutResult"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_rollout_evidence.arn}/rollout-results/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  statement {
    sid       = "DenyWrongRolloutEncryptionKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_rollout_evidence.arn}/rollout-results/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.openclaw_rollout_evidence.arn]
    }
  }

  statement {
    sid       = "DenyRolloutResultWithoutComplianceLock"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_rollout_evidence.arn}/rollout-results/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:object-lock-mode"
      values   = ["COMPLIANCE"]
    }
  }

  statement {
    sid    = "DenyRolloutResultDeletion"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.openclaw_rollout_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "openclaw_rollout_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id
  policy = data.aws_iam_policy_document.openclaw_rollout_evidence_bucket.json
}

data "aws_iam_policy_document" "runtime_automation_openclaw_rollout" {
  statement {
    sid = "RunAndRollbackExactOpenClawService"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
      "ecs:RunTask",
      "ecs:UpdateService",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ReadExactOpenClawRolloutLogsAndCanarySecret"
    actions = [
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "arn:aws:logs:ap-northeast-1:718959508629:log-group:/teamagent/dev/openclaw:*",
      "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/openclaw/rollout-canary-*",
    ]
  }

  statement {
    sid = "ReadExactOpenClawRolloutBucketControls"
    actions = [
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
      "s3:GetEncryptionConfiguration",
    ]
    resources = [aws_s3_bucket.openclaw_rollout_evidence.arn]
  }

  statement {
    sid = "WriteAndVerifyOnlyOpenClawRolloutResults"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.openclaw_rollout_evidence.arn}/rollout-results/*",
    ]
  }

  statement {
    sid = "UseOnlyOpenClawRolloutEncryptionKey"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = ["arn:aws:kms:ap-northeast-1:718959508629:key/*"]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "kms:ResourceAliases"
      values   = [local.openclaw_rollout_evidence_alias]
    }
  }

  statement {
    sid = "SignAndVerifyOnlyOpenClawRolloutResults"
    actions = [
      "kms:DescribeKey",
      "kms:GetPublicKey",
      "kms:Sign",
      "kms:Verify",
    ]
    resources = ["arn:aws:kms:ap-northeast-1:718959508629:key/*"]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "kms:ResourceAliases"
      values   = [local.openclaw_rollout_signing_alias]
    }
  }

  statement {
    sid = "BindOneUseOpenClawRollbackAuthorization"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "intent#*",
        "lock#teamagent/terraform.tfstate",
        "openclaw-rollback#*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "runtime_automation_openclaw_rollout" {
  name   = "${local.runtime_automation_role_name}-openclaw-rollout"
  role   = aws_iam_role.runtime_automation.id
  policy = data.aws_iam_policy_document.runtime_automation_openclaw_rollout.json
}

check "openclaw_rollout_evidence_contract" {
  assert {
    condition = (
      aws_s3_bucket.openclaw_rollout_evidence.bucket ==
      "teamagent-dev-openclaw-rollout-evidence" &&
      aws_s3_bucket.openclaw_rollout_evidence.object_lock_enabled &&
      aws_kms_alias.openclaw_rollout_evidence.name ==
      "alias/teamagent-dev-openclaw-rollout-evidence" &&
      aws_kms_alias.openclaw_rollout_signing.name ==
      "alias/teamagent-dev-openclaw-rollout-signing" &&
      aws_kms_key.openclaw_rollout_evidence.key_usage ==
      "ENCRYPT_DECRYPT" &&
      aws_kms_key.openclaw_rollout_signing.key_usage == "SIGN_VERIFY" &&
      aws_kms_key.openclaw_rollout_signing.customer_master_key_spec == "RSA_3072"
    )
    error_message = "OpenClaw rollout evidence must use the fixed Object Lock bucket and asymmetric KMS signer."
  }
}

output "openclaw_rollout_evidence_bucket" {
  value = aws_s3_bucket.openclaw_rollout_evidence.id
}

output "openclaw_rollout_evidence_key_arn" {
  value = aws_kms_key.openclaw_rollout_evidence.arn
}

output "openclaw_rollout_signing_key_arn" {
  value = aws_kms_key.openclaw_rollout_signing.arn
}

# ============================================================
# Forced-rollback drill evidence and independent signing authority
# ============================================================
# The operational rollout-evidence bucket already has Object Lock, versioning,
# and default SSE-KMS. Drill objects use a separate prefix and explicitly opt
# into the stronger per-object COMPLIANCE retention contract.

locals {
  forced_rollback_drill_evidence_bucket_name = "teamagent-dev-openclaw-rollout-evidence"
  forced_rollback_drill_evidence_prefix      = "forced-rollback-drills/"
  forced_rollback_drill_object_lock_mode     = "COMPLIANCE"
  forced_rollback_drill_retention_days       = 3650
  forced_rollback_drill_signing_algorithm    = "RSASSA_PSS_SHA_256"
  forced_rollback_drill_signing_key_alias    = "alias/teamagent-dev-forced-rollback-drill-signing"
  forced_rollback_drill_role_name            = "teamagent-dev-forced-rollback-drill"
  forced_rollback_drill_role_session_name    = "teamagent-forced-rollback-drill"
  forced_rollback_drill_role_arn = (
    "arn:aws:iam::${local.expected_build_account_id}:role/${local.forced_rollback_drill_role_name}"
  )
  forced_rollback_drill_evidence_object_arn = (
    "${aws_s3_bucket.openclaw_rollout_evidence.arn}/${local.forced_rollback_drill_evidence_prefix}*"
  )
}

data "aws_iam_policy_document" "forced_rollback_drill_signing_key" {
  statement {
    sid    = "AllowForcedRollbackDrillKeyAdministration"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.expected_build_account_id}:root"]
    }
    actions = [
      "kms:CancelKeyDeletion",
      "kms:CreateAlias",
      "kms:CreateGrant",
      "kms:DeleteAlias",
      "kms:DescribeKey",
      "kms:DisableKey",
      "kms:EnableKey",
      "kms:GetKeyPolicy",
      "kms:GetPublicKey",
      "kms:ListGrants",
      "kms:ListKeyPolicies",
      "kms:ListResourceTags",
      "kms:ListRetirableGrants",
      "kms:PutKeyPolicy",
      "kms:RevokeGrant",
      "kms:ScheduleKeyDeletion",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateAlias",
      "kms:UpdateKeyDescription",
      "kms:UpdatePrimaryRegion",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "AllowOnlyForcedRollbackDrillAggregateSigning"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.expected_build_account_id}:root"]
    }
    actions = [
      "kms:Sign",
      "kms:Verify",
    ]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.forced_rollback_drill_role_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:SigningAlgorithm"
      values   = [local.forced_rollback_drill_signing_algorithm]
    }
  }

  statement {
    sid    = "AllowForcedRollbackDrillPublicKeyInspection"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.expected_build_account_id}:root"]
    }
    actions = [
      "kms:DescribeKey",
      "kms:GetPublicKey",
    ]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.forced_rollback_drill_role_arn]
    }
  }
}

resource "aws_kms_key" "forced_rollback_drill_signing" {
  description              = "Sign immutable forced-rollback drill aggregates"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"
  policy                   = data.aws_iam_policy_document.forced_rollback_drill_signing_key.json

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "forced_rollback_drill_signing" {
  name          = local.forced_rollback_drill_signing_key_alias
  target_key_id = aws_kms_key.forced_rollback_drill_signing.key_id
}

resource "aws_s3_bucket_lifecycle_configuration" "forced_rollback_drill_evidence" {
  bucket = aws_s3_bucket.openclaw_rollout_evidence.id

  depends_on = [
    aws_s3_bucket_object_lock_configuration.openclaw_rollout_evidence,
    aws_s3_bucket_server_side_encryption_configuration.openclaw_rollout_evidence,
    aws_s3_bucket_versioning.openclaw_rollout_evidence,
  ]

  rule {
    id     = "forced-rollback-drills-immutable-evidence"
    status = "Enabled"

    filter {
      prefix = local.forced_rollback_drill_evidence_prefix
    }

    # Retained versions are never expired by lifecycle. Only incomplete uploads
    # and already-expired delete markers are cleaned up.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  lifecycle {
    precondition {
      condition = (
        aws_s3_bucket.openclaw_rollout_evidence.bucket ==
        local.forced_rollback_drill_evidence_bucket_name &&
        aws_s3_bucket.openclaw_rollout_evidence.object_lock_enabled
      )
      error_message = "The existing forced-rollback drill evidence bucket must retain its fixed name and have Object Lock enabled."
    }

    precondition {
      condition = (
        aws_s3_bucket_versioning.openclaw_rollout_evidence.versioning_configuration[0].status ==
        "Enabled"
      )
      error_message = "The existing forced-rollback drill evidence bucket must remain versioned."
    }
  }
}

data "aws_iam_policy_document" "forced_rollback_drill_assume" {
  statement {
    sid     = "ExactAIIAdevMfaForcedRollbackDrillSession"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }

    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = [local.forced_rollback_drill_role_session_name]
    }
  }
}

data "aws_iam_policy_document" "forced_rollback_drill_boundary" {
  statement {
    sid       = "AllowOnlyIdentityPolicyIntersection"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid    = "DenyIamSelfEscalation"
    effect = "Deny"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:CreateRole",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:DeleteRole",
      "iam:DeleteRolePermissionsBoundary",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePermissionsBoundary",
      "iam:PutRolePolicy",
      "iam:SetDefaultPolicyVersion",
      "iam:TagPolicy",
      "iam:TagRole",
      "iam:UntagPolicy",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "DenyReleaseApprovalKeySigning"
    effect    = "Deny"
    actions   = ["kms:Sign"]
    resources = [aws_kms_key.approval_signing.arn]
  }

  statement {
    sid       = "DenyWeakerForcedRollbackDrillRetentionMode"
    effect    = "Deny"
    actions   = ["s3:PutObjectRetention"]
    resources = [local.forced_rollback_drill_evidence_object_arn]
    condition {
      test     = "StringNotEquals"
      variable = "s3:object-lock-mode"
      values   = [local.forced_rollback_drill_object_lock_mode]
    }
  }

  statement {
    sid       = "DenyShorterForcedRollbackDrillRetentionPeriod"
    effect    = "Deny"
    actions   = ["s3:PutObjectRetention"]
    resources = [local.forced_rollback_drill_evidence_object_arn]
    condition {
      test     = "NumericLessThan"
      variable = "s3:object-lock-remaining-retention-days"
      values   = [tostring(local.forced_rollback_drill_retention_days)]
    }
  }

  statement {
    sid    = "DenyForcedRollbackDrillEvidenceDeletion"
    effect = "Deny"
    actions = [
      "s3:BypassGovernanceRetention",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [local.forced_rollback_drill_evidence_object_arn]
  }

  statement {
    sid    = "DenyForcedRollbackDrillBucketControlMutation"
    effect = "Deny"
    actions = [
      "s3:DeleteBucketPolicy",
      "s3:PutBucketLifecycleConfiguration",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutBucketPolicy",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
    ]
    resources = [aws_s3_bucket.openclaw_rollout_evidence.arn]
  }
}

resource "aws_iam_policy" "forced_rollback_drill_boundary" {
  name        = "${local.forced_rollback_drill_role_name}-boundary"
  description = "Prevent drill evidence weakening, release-key signing, and IAM self-escalation"
  policy      = data.aws_iam_policy_document.forced_rollback_drill_boundary.json

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = length(replace(data.aws_iam_policy_document.forced_rollback_drill_boundary.json, "/\\s/", "")) < 6144
      error_message = "Forced-rollback drill permissions boundary must remain below 6,144 non-whitespace characters."
    }
  }
}

resource "aws_iam_role" "forced_rollback_drill" {
  name                 = local.forced_rollback_drill_role_name
  assume_role_policy   = data.aws_iam_policy_document.forced_rollback_drill_assume.json
  max_session_duration = 10800
  permissions_boundary = aws_iam_policy.forced_rollback_drill_boundary.arn
}

data "aws_iam_policy_document" "forced_rollback_drill" {
  statement {
    sid = "ReadForcedRollbackDrillBucketControls"
    actions = [
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
      "s3:GetEncryptionConfiguration",
    ]
    resources = [aws_s3_bucket.openclaw_rollout_evidence.arn]
  }

  statement {
    sid       = "ListOnlyForcedRollbackDrillEvidence"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.openclaw_rollout_evidence.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.forced_rollback_drill_evidence_prefix}*"]
    }
  }

  statement {
    sid = "ReadOnlyForcedRollbackDrillEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [local.forced_rollback_drill_evidence_object_arn]
  }

  statement {
    sid       = "PutOnlyCompliantForcedRollbackDrillEvidence"
    actions   = ["s3:PutObject"]
    resources = [local.forced_rollback_drill_evidence_object_arn]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.openclaw_rollout_evidence.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:object-lock-mode"
      values   = [local.forced_rollback_drill_object_lock_mode]
    }
    condition {
      test     = "NumericGreaterThanEquals"
      variable = "s3:object-lock-remaining-retention-days"
      values   = [tostring(local.forced_rollback_drill_retention_days)]
    }
  }

  statement {
    sid       = "ExtendOnlyCompliantForcedRollbackDrillRetention"
    actions   = ["s3:PutObjectRetention"]
    resources = [local.forced_rollback_drill_evidence_object_arn]
    condition {
      test     = "StringEquals"
      variable = "s3:object-lock-mode"
      values   = [local.forced_rollback_drill_object_lock_mode]
    }
    condition {
      test     = "NumericGreaterThanEquals"
      variable = "s3:object-lock-remaining-retention-days"
      values   = [tostring(local.forced_rollback_drill_retention_days)]
    }
  }

  statement {
    sid = "UseOnlyExistingRolloutEvidenceEncryptionKey"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.openclaw_rollout_evidence.arn]
  }

  statement {
    sid = "InspectForcedRollbackDrillSigningKey"
    actions = [
      "kms:DescribeKey",
      "kms:GetPublicKey",
    ]
    resources = [aws_kms_key.forced_rollback_drill_signing.arn]
  }

  statement {
    sid = "SignAndVerifyOnlyForcedRollbackDrillAggregates"
    actions = [
      "kms:Sign",
      "kms:Verify",
    ]
    resources = [aws_kms_key.forced_rollback_drill_signing.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:SigningAlgorithm"
      values   = [local.forced_rollback_drill_signing_algorithm]
    }
  }

  statement {
    sid    = "DenyWritesOutsideForcedRollbackDrillPrefix"
    effect = "Deny"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject*",
      "s3:PutObject*",
      "s3:RestoreObject",
    ]
    not_resources = [local.forced_rollback_drill_evidence_object_arn]
  }
}

resource "aws_iam_role_policy" "forced_rollback_drill" {
  name   = local.forced_rollback_drill_role_name
  role   = aws_iam_role.forced_rollback_drill.id
  policy = data.aws_iam_policy_document.forced_rollback_drill.json
}

output "forced_rollback_drill_evidence_bucket" {
  value = aws_s3_bucket.openclaw_rollout_evidence.id
}

output "forced_rollback_drill_evidence_prefix" {
  value = local.forced_rollback_drill_evidence_prefix
}

output "forced_rollback_drill_signing_key_arn" {
  value = aws_kms_key.forced_rollback_drill_signing.arn
}

output "forced_rollback_drill_role_arn" {
  value = aws_iam_role.forced_rollback_drill.arn
}

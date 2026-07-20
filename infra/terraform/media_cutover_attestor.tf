# ============================================================
# Media cutover: independent one-use attestation authority
# ============================================================
# Runtime automation is intentionally unable to mint authoritative media
# cutover rows or sign their claims.  A short-lived MFA session on this role
# independently re-reads the exact quiesced runtime, signs the intent-bound
# claims, and conditionally creates the READY row.  Runtime automation may
# only read and verify that row.

locals {
  media_cutover_attestor_key_alias = (
    "alias/${var.project_name}-${var.environment}-media-cutover-attestor"
  )
  media_cutover_attestor_role_name = (
    "${var.project_name}-${var.environment}-media-cutover-attestor"
  )
  media_cutover_attestor_session_name = "teamagent-media-cutover-attestor"
  media_cutover_attestor_source_identity = (
    "teamagent-production-media-cutover-attestor"
  )
}

resource "aws_kms_key" "media_cutover_attestor" {
  description              = "TeamAgent intent-bound one-use media cutover attestations"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
  deletion_window_in_days  = 30

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "media_cutover_attestor" {
  name          = local.media_cutover_attestor_key_alias
  target_key_id = aws_kms_key.media_cutover_attestor.key_id
}

data "aws_iam_policy_document" "media_cutover_attestor_assume" {
  statement {
    sid = "ExactRootMfaMediaCutoverSession"
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::718959508629:root"]
    }

    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = [local.media_cutover_attestor_session_name]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = [local.media_cutover_attestor_source_identity]
    }
  }
}

resource "aws_iam_role" "media_cutover_attestor" {
  name                 = local.media_cutover_attestor_role_name
  assume_role_policy   = data.aws_iam_policy_document.media_cutover_attestor_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "media_cutover_attestor" {
  statement {
    sid = "ReadExactMediaCutoverRuntime"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:ListTasks",
      "lambda:GetFunctionConfiguration",
      "lambda:ListEventSourceMappings",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = ["*"]
  }

  statement {
    sid = "SignOnlyMediaCutoverClaims"
    actions = [
      "kms:DescribeKey",
      "kms:GetPublicKey",
      "kms:Sign",
      "kms:Verify",
    ]
    resources = [aws_kms_key.media_cutover_attestor.arn]
  }

  statement {
    sid = "CreateOnlyUniqueMediaCutoverReadyRow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["media-cutover#*"]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  statement {
    sid = "AtomicallyAuthorizeOneMediaApply"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "intent#*",
        "lock#teamagent/terraform.tfstate",
        "media-cutover#*",
      ]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyDeploymentAndRuntimeMutation"
    effect = "Deny"
    actions = [
      "codebuild:*",
      "ecr:*",
      "ecs:CreateService",
      "ecs:DeleteService",
      "ecs:RegisterTaskDefinition",
      "ecs:RunTask",
      "ecs:StopTask",
      "ecs:UpdateService",
      "events:*",
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:UpdateEventSourceMapping",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "s3:*",
      "scheduler:*",
      "secretsmanager:*",
      "sns:Publish",
      "sqs:DeleteMessage",
      "sqs:PurgeQueue",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
    ]
    resources = ["*"]
  }

  statement {
    sid           = "DenySigningWithAnyOtherKey"
    effect        = "Deny"
    actions       = ["kms:Sign"]
    not_resources = [aws_kms_key.media_cutover_attestor.arn]
  }

  statement {
    sid    = "DenyOtherLedgerMutation"
    effect = "Deny"
    actions = [
      "dynamodb:BatchWriteItem",
      "dynamodb:DeleteItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
  }
}

resource "aws_iam_role_policy" "media_cutover_attestor" {
  name   = local.media_cutover_attestor_role_name
  role   = aws_iam_role.media_cutover_attestor.id
  policy = data.aws_iam_policy_document.media_cutover_attestor.json
}

check "media_cutover_attestor_preconditions" {
  assert {
    condition = (
      aws_kms_alias.media_cutover_attestor.name ==
      local.media_cutover_attestor_key_alias &&
      aws_kms_key.media_cutover_attestor.key_usage == "SIGN_VERIFY" &&
      aws_kms_key.media_cutover_attestor.customer_master_key_spec ==
      "ECC_NIST_P256" &&
      aws_iam_role.media_cutover_attestor.arn ==
      "arn:aws:iam::718959508629:role/${local.media_cutover_attestor_role_name}"
    )
    error_message = "Independent media cutover attestor ownership differs."
  }
}

output "media_cutover_attestor_key_arn" {
  value = aws_kms_key.media_cutover_attestor.arn
}

output "media_cutover_attestor_role_arn" {
  value = aws_iam_role.media_cutover_attestor.arn
}

output "media_cutover_attestor_assume_policy_contract" {
  value = data.aws_iam_policy_document.media_cutover_attestor_assume.json
}

output "media_cutover_attestor_policy_contract" {
  value = data.aws_iam_policy_document.media_cutover_attestor.json
}

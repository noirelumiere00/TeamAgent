# ============================================================
# Runtime evidence: externally bootstrapped acknowledgement and ledger contract
# ============================================================
# The durable record store is the same one-use intent/shared-lock table used by
# the production image gate. Runtime evidence rows use disjoint record_id
# prefixes (`sns-challenge#` and `alarm-migration#`) and conditional writes.
#
# These identities used to be external prerequisites, which made the first
# admissible full saved plan depend on authority that only that plan could
# create. The one-time create-only bootstrap now creates these resources
# directly in this main state under a temporary STS session. There is no second
# Terraform state owning them, and the temporary seed is retired after the
# main-state serial/address handoff is verified. No access key or login profile
# is created.

locals {
  alarm_recipient_ack_key_alias = (
    "alias/${var.project_name}-${var.environment}-alarm-recipient-ack"
  )
  alarm_recipient_ack_signer_role_name = (
    "${var.project_name}-${var.environment}-alarm-recipient-ack-signer"
  )
  alarm_recipient_ack_session_name = "teamagent-alarm-recipient-ack"
  alarm_recipient_ack_source_identity = (
    "teamagent-production-alarm-recipient"
  )
  # This external managed identity represents the approved recipient. It must
  # itself be backed by the organization's SSO/MFA control plane; this state
  # neither creates a user/access key nor grants administrator permissions.
  alarm_recipient_identity_role_arn = (
    "arn:aws:iam::718959508629:role/teamagent-dev-alarm-recipient-s-komata"
  )
  runtime_automation_role_name = (
    "${var.project_name}-${var.environment}-terraform-runtime-automation"
  )
  runtime_automation_session_name = "teamagent-terraform-worker"
  runtime_automation_source_identity = (
    "teamagent-production-terraform"
  )
  runtime_automation_role_arn = (
    "arn:aws:iam::718959508629:role/${local.runtime_automation_role_name}"
  )
  alarm_topic_arn = (
    "arn:aws:sns:${var.aws_region}:718959508629:${var.project_name}-${var.environment}-openclaw-alarms"
  )
}

resource "aws_kms_key" "alarm_recipient_ack" {
  description              = "TeamAgent exact alarm-recipient acknowledgement signatures"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
  deletion_window_in_days  = 30

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "alarm_recipient_ack" {
  name          = local.alarm_recipient_ack_key_alias
  target_key_id = aws_kms_key.alarm_recipient_ack.key_id
}

data "aws_iam_policy_document" "alarm_recipient_ack_signer_assume" {
  statement {
    sid     = "ExistingOrganizationRecipientRole"
    actions = ["sts:AssumeRole"]

    # Account-root plus PrincipalArn keeps the policy creatable before the
    # organization role is visible while preserving the former exact
    # organization-role path.
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.alarm_recipient_identity_role_arn]
    }
  }

  statement {
    sid = "ExactRootMfaRecipientSession"
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
      values   = [local.alarm_recipient_ack_session_name]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = [local.alarm_recipient_ack_source_identity]
    }
  }
}

resource "aws_iam_role" "alarm_recipient_ack_signer" {
  name                 = local.alarm_recipient_ack_signer_role_name
  assume_role_policy   = data.aws_iam_policy_document.alarm_recipient_ack_signer_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "runtime_automation_assume" {
  statement {
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
      values   = [local.runtime_automation_session_name]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = [local.runtime_automation_source_identity]
    }
  }
}

resource "aws_iam_role" "runtime_automation" {
  name                 = local.runtime_automation_role_name
  assume_role_policy   = data.aws_iam_policy_document.runtime_automation_assume.json
  max_session_duration = 10800
}

resource "aws_iam_role_policy_attachment" "runtime_automation_power_user" {
  role       = aws_iam_role.runtime_automation.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"

  # Never expose PowerUserAccess before the inline provenance/build/seed
  # denials are attached to this assumable role.
  depends_on = [aws_iam_role_policy.runtime_automation_control_plane]
}

data "aws_iam_policy_document" "alarm_recipient_ack_signer" {
  statement {
    sid       = "SignOnlyRecipientAcknowledgement"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign"]
    resources = [aws_kms_key.alarm_recipient_ack.arn]
  }

  statement {
    sid    = "DenyOtherMutation"
    effect = "Deny"
    actions = [
      "dynamodb:*",
      "ecs:*",
      "events:*",
      "lambda:*",
      "s3:*",
      "scheduler:*",
      "sns:Publish",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "alarm_recipient_ack_signer" {
  name   = local.alarm_recipient_ack_signer_role_name
  role   = aws_iam_role.alarm_recipient_ack_signer.id
  policy = data.aws_iam_policy_document.alarm_recipient_ack_signer.json
}

data "aws_iam_policy_document" "runtime_evidence_automation" {
  statement {
    sid = "InventoryAllKnownRuntimeAndSnsPublishers"
    actions = [
      "application-autoscaling:Describe*",
      "autoscaling:DescribeNotificationConfigurations",
      "bedrock:GetModelInvocationLoggingConfiguration",
      "budgets:Describe*",
      "chatbot:DescribeChimeWebhookConfigurations",
      "chatbot:DescribeSlackChannelConfigurations",
      "chatbot:ListMicrosoftTeamsChannelConfigurations",
      "cloudtrail:GetTrail",
      "cloudtrail:GetTrailStatus",
      "cloudwatch:DescribeAlarms",
      "codestar-notifications:DescribeNotificationRule",
      "codestar-notifications:ListNotificationRules",
      "ce:GetAnomalySubscriptions",
      "ec2:Describe*",
      "ecs:DescribeServices",
      "ecs:ListTasks",
      "events:ListRules",
      "events:ListEventBuses",
      "events:ListTargetsByRule",
      "lambda:GetFunctionConfiguration",
      "lambda:ListEventSourceMappings",
      "lambda:ListFunctionEventInvokeConfigs",
      "lambda:ListFunctions",
      "logs:DescribeMetricFilters",
      "rds:DescribeEventSubscriptions",
      "s3:GetBucketLifecycleConfiguration",
      "s3:GetBucketNotification",
      "s3:GetBucketPolicy",
      "s3:GetBucketVersioning",
      "s3:GetObjectVersion",
      "s3:GetObjectVersionAttributes",
      "s3:ListAllMyBuckets",
      "s3:ListBucketVersions",
      "scheduler:GetSchedule",
      "scheduler:ListScheduleGroups",
      "scheduler:ListSchedules",
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTopics",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ListQueues",
    ]
    resources = ["*"]
  }

  statement {
    sid = "FirstTimeVersioningAndWriterQuiescence"
    actions = [
      "bedrock:PutModelInvocationLoggingConfiguration",
      "bedrock:DeleteModelInvocationLoggingConfiguration",
      "cloudtrail:StartLogging",
      "cloudtrail:StopLogging",
      "ecs:StopTask",
      "ecs:UpdateService",
      "events:DisableRule",
      "lambda:UpdateEventSourceMapping",
      "s3:PutBucketVersioning",
      "scheduler:UpdateSchedule",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PublishOnlyCanonicalAlarmChallenge"
    actions   = ["sns:Publish"]
    resources = [local.alarm_topic_arn]
  }

  statement {
    sid       = "VerifyOnlyRecipientAckKey"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.alarm_recipient_ack.arn]
  }

  statement {
    sid = "ConditionalRuntimeEvidenceLedger"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "alarm-migration#*",
        "lock#teamagent/terraform.tfstate",
        "sns-challenge#*",
        "versioning-cutover#*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "runtime_evidence_automation" {
  name   = "${local.runtime_automation_role_name}-evidence"
  role   = aws_iam_role.runtime_automation.id
  policy = data.aws_iam_policy_document.runtime_evidence_automation.json
}

data "aws_iam_policy_document" "runtime_automation_control_plane" {
  statement {
    sid = "ReadIamMetadata"
    actions = [
      "iam:Get*",
      "iam:List*",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ManageOnlyTeamAgentIam"
    actions = [
      "iam:AddRoleToInstanceProfile",
      "iam:AttachRolePolicy",
      "iam:CreateInstanceProfile",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:CreateRole",
      "iam:CreateUser",
      "iam:DeleteInstanceProfile",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DeleteUser",
      "iam:DeleteUserPolicy",
      "iam:DetachRolePolicy",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:GetUser",
      "iam:GetUserPolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListPolicyVersions",
      "iam:ListRolePolicies",
      "iam:ListRoles",
      "iam:ListUserPolicies",
      "iam:ListUsers",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:PutUserPolicy",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:TagInstanceProfile",
      "iam:TagPolicy",
      "iam:TagRole",
      "iam:TagUser",
      "iam:UntagInstanceProfile",
      "iam:UntagPolicy",
      "iam:UntagRole",
      "iam:UntagUser",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateUser",
    ]
    resources = [
      "arn:aws:iam::718959508629:instance-profile/teamagent-*",
      "arn:aws:iam::718959508629:role/teamagent-*",
      "arn:aws:iam::718959508629:user/AIIAdev",
      "arn:aws:iam::718959508629:user/teamagent-*",
      "arn:aws:iam::718959508629:policy/teamagent-*",
    ]
  }

  statement {
    sid = "CreateOnlyRequiredAwsServiceLinkedRoles"
    actions = [
      "iam:CreateServiceLinkedRole",
      "iam:GetServiceLinkedRoleDeletionStatus",
    ]
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values = [
        "*.amazonaws.com",
      ]
    }
  }

  statement {
    sid = "UseExactTerraformBackend"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]
    resources = ["arn:aws:s3:::teamagent-tfstate-718959508629"]
  }

  statement {
    sid = "ReadWriteExactTerraformStateObject"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "arn:aws:s3:::teamagent-tfstate-718959508629/teamagent/terraform.tfstate",
    ]
  }

  statement {
    sid = "UseExactTerraformBackendLock"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [
      "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock",
    ]
  }

  statement {
    sid    = "DenyBootstrapAuditMutation"
    effect = "Deny"
    actions = [
      "dynamodb:BatchWriteItem",
      "dynamodb:DeleteItem",
      "dynamodb:PartiQLDelete",
      "dynamodb:PartiQLInsert",
      "dynamodb:PartiQLUpdate",
      "dynamodb:PutItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [
      "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock",
    ]

    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["bootstrap#teamagent-production-provenance-iam-v1"]
    }
  }

  statement {
    sid     = "DenyBootstrapAuditTableDeletion"
    effect  = "Deny"
    actions = ["dynamodb:DeleteTable"]
    resources = [
      "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock",
    ]
  }

  statement {
    sid     = "DenyBootstrapSeedIamMutation"
    effect  = "Deny"
    actions = ["iam:*"]
    resources = [
      "arn:aws:iam::718959508629:role/teamagent-production-provenance-bootstrap-v1",
      "arn:aws:iam::718959508629:policy/teamagent-production-provenance-bootstrap-deny-v1",
    ]
  }

  statement {
    sid       = "AssumeOnlyImageDeploymentGate"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.image_deployment_gate.arn]
  }

  statement {
    sid    = "DenyLongLivedHumanCredentials"
    effect = "Deny"
    actions = [
      "iam:CreateAccessKey",
      "iam:CreateLoginProfile",
      "iam:CreateServiceSpecificCredential",
      "iam:ResetServiceSpecificCredential",
      "iam:UpdateAccessKey",
      "iam:UpdateLoginProfile",
      "iam:UploadSSHPublicKey",
      "iam:UploadSigningCertificate",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyBuildReleaseAndDebugExecution"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyImageAndSigningMutation"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "kms:GenerateMac",
      "kms:ScheduleKeyDeletion",
      "kms:Sign",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyReleaseEvidenceObjectMutation"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "arn:aws:s3:::teamagent-dev-image-release-evidence/*",
      "arn:aws:s3:::teamagent-dev-openclaw-build-evidence/*",
    ]
  }
}

resource "aws_iam_role_policy" "runtime_automation_control_plane" {
  name   = "${local.runtime_automation_role_name}-control-plane"
  role   = aws_iam_role.runtime_automation.id
  policy = data.aws_iam_policy_document.runtime_automation_control_plane.json
}

check "runtime_evidence_owned_preconditions" {
  assert {
    condition = (
      aws_kms_key.alarm_recipient_ack.key_usage == "SIGN_VERIFY" &&
      aws_kms_key.alarm_recipient_ack.customer_master_key_spec == "ECC_NIST_P256" &&
      aws_iam_role.alarm_recipient_ack_signer.arn ==
      "arn:aws:iam::718959508629:role/teamagent-dev-alarm-recipient-ack-signer" &&
      aws_iam_role.runtime_automation.arn ==
      local.runtime_automation_role_arn
    )
    error_message = "Runtime evidence requires the exact main-state KMS signer and STS-only automation roles."
  }
}

output "alarm_recipient_ack_key_arn" {
  value = aws_kms_key.alarm_recipient_ack.arn
}

output "alarm_recipient_ack_signer_role_arn" {
  value = aws_iam_role.alarm_recipient_ack_signer.arn
}

output "runtime_automation_role_arn" {
  value = aws_iam_role.runtime_automation.arn
}

output "alarm_recipient_ack_signer_assume_policy_contract" {
  value = data.aws_iam_policy_document.alarm_recipient_ack_signer_assume.json
}

output "alarm_recipient_ack_signer_policy_contract" {
  value = data.aws_iam_policy_document.alarm_recipient_ack_signer.json
}

output "runtime_evidence_automation_policy_contract" {
  value = data.aws_iam_policy_document.runtime_evidence_automation.json
}

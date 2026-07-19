# ============================================================
# Runtime evidence: externally bootstrapped acknowledgement and ledger contract
# ============================================================
# The durable record store is the same one-use intent/shared-lock table used by
# the production image gate. Runtime evidence rows use disjoint record_id
# prefixes (`sns-challenge#` and `alarm-migration#`) and conditional writes.
#
# These identities and permissions are deliberate external prerequisites. The
# first-time versioning and SNS workflows run before this state may produce an
# admissible full saved plan, so creating their own authority in that plan would
# be a bootstrap cycle. The organization-managed KMS/SSO roles and the runtime
# automation role policy must be independently provisioned and reviewed first.
# This state reads and pins them but never creates a user, access key, or role.

locals {
  alarm_recipient_ack_key_alias = (
    "alias/${var.project_name}-${var.environment}-alarm-recipient-ack"
  )
  alarm_recipient_ack_signer_role_name = (
    "${var.project_name}-${var.environment}-alarm-recipient-ack-signer"
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
}

data "aws_kms_alias" "alarm_recipient_ack" {
  name = local.alarm_recipient_ack_key_alias
}

data "aws_iam_role" "alarm_recipient_ack_signer" {
  name = local.alarm_recipient_ack_signer_role_name
}

data "aws_iam_role" "runtime_automation" {
  name = local.runtime_automation_role_name
}

check "runtime_evidence_external_preconditions" {
  assert {
    condition = (
      can(regex(
        "^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-fA-F-]{36}$",
        data.aws_kms_alias.alarm_recipient_ack.target_key_arn,
      )) &&
      data.aws_iam_role.alarm_recipient_ack_signer.arn ==
      "arn:aws:iam::718959508629:role/teamagent-dev-alarm-recipient-ack-signer" &&
      data.aws_iam_role.runtime_automation.arn ==
      "arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation"
    )
    error_message = "Runtime evidence requires the exact external KMS/SSO signer and automation roles."
  }
}

data "aws_iam_policy_document" "alarm_recipient_ack_signer_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [local.alarm_recipient_identity_role_arn]
    }

  }
}

data "aws_iam_policy_document" "alarm_recipient_ack_signer" {
  statement {
    sid       = "SignOnlyRecipientAcknowledgement"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign"]
    resources = [data.aws_kms_alias.alarm_recipient_ack.target_key_arn]
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
    resources = [aws_sns_topic.alarms.arn]
  }

  statement {
    sid       = "VerifyOnlyRecipientAckKey"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [data.aws_kms_alias.alarm_recipient_ack.target_key_arn]
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

output "alarm_recipient_ack_key_arn" {
  value = data.aws_kms_alias.alarm_recipient_ack.target_key_arn
}

output "alarm_recipient_ack_signer_role_arn" {
  value = data.aws_iam_role.alarm_recipient_ack_signer.arn
}

output "runtime_automation_role_arn" {
  value = data.aws_iam_role.runtime_automation.arn
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

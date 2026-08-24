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

  # Account root cannot assume roles, and the organization SCP refuses
  # sts:SetSourceIdentity. The live AIIAdev principal uses AssumeRole while MFA
  # and the exact session name stay required.
  statement {
    sid     = "ExactAIIAdevMfaRecipientSession"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [data.aws_iam_user.aiia_dev.arn]
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
  }
}

resource "aws_iam_role" "alarm_recipient_ack_signer" {
  name                 = local.alarm_recipient_ack_signer_role_name
  assume_role_policy   = data.aws_iam_policy_document.alarm_recipient_ack_signer_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "runtime_automation_assume" {
  # Account root cannot assume roles, and the organization SCP refuses
  # sts:SetSourceIdentity. The live AIIAdev principal uses AssumeRole while MFA
  # and the exact session name stay required.
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [data.aws_iam_user.aiia_dev.arn]
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
  }
}

data "aws_iam_policy_document" "runtime_automation_boundary" {
  statement {
    sid       = "AllowOnlyIdentityPolicyIntersection"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid    = "DenyIamSelfEscalation"
    effect = "Deny"
    actions = [
      "iam:AddRoleToInstanceProfile",
      "iam:AttachGroupPolicy",
      "iam:AttachRolePolicy",
      "iam:AttachUserPolicy",
      "iam:CreateAccessKey",
      "iam:CreateGroup",
      "iam:CreateInstanceProfile",
      "iam:CreateLoginProfile",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:CreateRole",
      "iam:CreateServiceLinkedRole",
      "iam:CreateServiceSpecificCredential",
      "iam:CreateUser",
      "iam:DeleteGroup",
      "iam:DeleteGroupPolicy",
      "iam:DeleteInstanceProfile",
      "iam:DeleteLoginProfile",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:DeleteRole",
      "iam:DeleteRolePermissionsBoundary",
      "iam:DeleteRolePolicy",
      "iam:DeleteServiceLinkedRole",
      "iam:DeleteServiceSpecificCredential",
      "iam:DeleteUser",
      "iam:DeleteUserPermissionsBoundary",
      "iam:DeleteUserPolicy",
      "iam:DetachGroupPolicy",
      "iam:DetachRolePolicy",
      "iam:DetachUserPolicy",
      "iam:PutGroupPolicy",
      "iam:PutRolePermissionsBoundary",
      "iam:PutRolePolicy",
      "iam:PutUserPermissionsBoundary",
      "iam:PutUserPolicy",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:ResetServiceSpecificCredential",
      "iam:SetDefaultPolicyVersion",
      "iam:TagInstanceProfile",
      "iam:TagPolicy",
      "iam:TagRole",
      "iam:TagUser",
      "iam:UntagInstanceProfile",
      "iam:UntagPolicy",
      "iam:UntagRole",
      "iam:UntagUser",
      "iam:UpdateAccessKey",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateGroup",
      "iam:UpdateLoginProfile",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateSAMLProvider",
      "iam:UpdateSigningCertificate",
      "iam:UpdateSSHPublicKey",
      "iam:UpdateUser",
      "iam:UploadSAMLProvider",
      "iam:UploadServerCertificate",
      "iam:UploadSigningCertificate",
      "iam:UploadSSHPublicKey",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyRoleChaining"
    effect = "Deny"
    actions = [
      "sts:AssumeRole",
      "sts:AssumeRoleWithSAML",
      "sts:AssumeRoleWithWebIdentity",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyAuthoritativeMediaLedgerMutation"
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
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAnyValue:StringLike"
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
    sid    = "DenyAuthoritativeLedgerControlPlaneMutation"
    effect = "Deny"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteResourcePolicy",
      "dynamodb:DeleteTable",
      "dynamodb:ImportTable",
      "dynamodb:PutResourcePolicy",
      "dynamodb:RestoreTableFromBackup",
      "dynamodb:RestoreTableToPointInTime",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateKinesisStreamingDestination",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTableReplicaAutoScaling",
      "dynamodb:UpdateTimeToLive",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
  }

  statement {
    sid    = "DenyMediaAttestorKeyMutationAndUse"
    effect = "Deny"
    actions = [
      "kms:CancelKeyDeletion",
      "kms:CreateGrant",
      "kms:Decrypt",
      "kms:DeleteImportedKeyMaterial",
      "kms:DisableKey",
      "kms:EnableKey",
      "kms:ImportKeyMaterial",
      "kms:PutKeyPolicy",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
      "kms:ReplicateKey",
      "kms:ScheduleKeyDeletion",
      "kms:Sign",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateKeyDescription",
      "kms:UpdatePrimaryRegion",
    ]
    resources = [aws_kms_key.media_cutover_attestor.arn]
  }

  statement {
    sid    = "DenyMediaAttestorAliasMutation"
    effect = "Deny"
    actions = [
      "kms:CreateAlias",
      "kms:DeleteAlias",
      "kms:UpdateAlias",
    ]
    resources = [
      "arn:aws:kms:${var.aws_region}:718959508629:${local.media_cutover_attestor_key_alias}",
      aws_kms_key.media_cutover_attestor.arn,
    ]
  }

  # Approval signing is an independent human authority.  The runtime role has
  # broad KMS control-plane permissions for ordinary Terraform resources, so
  # this key needs the same explicit permissions-boundary protection as the
  # media attestor rather than relying on its identity-policy allowlist.
  statement {
    sid    = "DenyApprovalKms"
    effect = "Deny"
    actions = [
      "kms:CancelKeyDeletion",
      "kms:CreateAlias",
      "kms:CreateGrant",
      "kms:DeleteAlias",
      "kms:DeleteImportedKeyMaterial",
      "kms:DisableKey",
      "kms:EnableKey",
      "kms:ImportKeyMaterial",
      "kms:PutKeyPolicy",
      "kms:ReplicateKey",
      "kms:ScheduleKeyDeletion",
      "kms:Sign",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:Update*",
      "kms:UpdateAlias",
    ]
    resources = [
      "arn:aws:kms:${var.aws_region}:${local.expected_build_account_id}:${local.approval_signing_key_alias}",
      aws_kms_key.approval_signing.arn,
    ]
  }

  statement {
    sid    = "DenyApprovalIam"
    effect = "Deny"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreatePolicyVersion",
      "iam:Delete*",
      "iam:DetachRolePolicy",
      "iam:PutRole*",
      "iam:SetDefaultPolicyVersion",
      "iam:Tag*",
      "iam:Untag*",
      "iam:Update*",
    ]
    resources = local.approval_runtime_iam_protected_arns
  }

  statement {
    sid    = "DenyApprovalObjects"
    effect = "Deny"
    actions = [
      "s3:DeleteObject*",
      "s3:PutObject*",
      "s3:RestoreObject",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*",
      "${aws_s3_bucket.image_release_evidence.arn}/codebuild-buildspecs/${local.approval_publisher_project_name}/*",
    ]
  }

  # Identity-policy object denies already stop writes, but preserving these
  # bucket controls prevents runtime from weakening the future-object contract.
  statement {
    sid    = "DenyApprovalBucketControls"
    effect = "Deny"
    actions = [
      "s3:DeleteBucketPolicy",
      "s3:PutBucketLifecycleConfiguration",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutBucketPolicy",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
    ]
    resources = [aws_s3_bucket.image_release_evidence.arn]
  }

  # Updating the project could point the exact signing role at an attacker
  # supplied inline buildspec even when the locked S3 object is immutable.
  statement {
    sid    = "DenyApprovalProject"
    effect = "Deny"
    actions = [
      "codebuild:DeleteProject",
      "codebuild:UpdateProject",
    ]
    resources = [local.approval_publisher_project_arn]
  }

}

resource "aws_iam_policy" "runtime_automation_boundary" {
  name        = "${local.runtime_automation_role_name}-boundary"
  description = "Immutable IAM self-escalation and role-chaining boundary"
  policy      = data.aws_iam_policy_document.runtime_automation_boundary.json

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = length(replace(data.aws_iam_policy_document.runtime_automation_boundary.json, "/\\s/", "")) < 6144
      error_message = "Runtime automation boundary must remain below 6,144 non-whitespace characters."
    }
  }
}

resource "aws_iam_role" "runtime_automation" {
  name                 = local.runtime_automation_role_name
  assume_role_policy   = data.aws_iam_policy_document.runtime_automation_assume.json
  max_session_duration = 10800
  permissions_boundary = aws_iam_policy.runtime_automation_boundary.arn
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
      "autoscaling:DescribeNotificationConfigurations",
      "aws-portal:ViewBilling",
      "bedrock:GetModelInvocationLoggingConfiguration",
      "budgets:ViewBudget",
      "chatbot:DescribeChimeWebhookConfigurations",
      "chatbot:DescribeSlackChannelConfigurations",
      "chatbot:ListMicrosoftTeamsChannelConfigurations",
      "cloudtrail:GetTrail",
      "cloudtrail:GetTrailStatus",
      "cloudwatch:DescribeAlarms",
      "codestar-notifications:DescribeNotificationRule",
      "codestar-notifications:ListNotificationRules",
      "ce:GetAnomalySubscriptions",
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
    sid       = "VerifyOnlyMediaCutoverAttestorKey"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.media_cutover_attestor.arn]
  }

  statement {
    sid       = "ReadOnlyAuthoritativeMediaCutoverLedger"
    actions   = ["dynamodb:GetItem"]
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
        "ecs-service-apply#*",
        "lock#teamagent/terraform.tfstate",
        "sns-challenge#*",
        "versioning-cutover#*",
      ]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  statement {
    sid = "AtomicallyFinalizeExactDeployment"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "apply-finalization#*",
        "apply-finalization-chunk#*",
        "ecs-service-apply#*",
        "intent#*",
        "lock#teamagent/terraform.tfstate",
      ]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  # The runtime session executes the deployment-intent helper directly. It
  # receives the helper's exact read/verify/ledger surface here instead of
  # chaining into a second role.
  statement {
    sid = "ReadExactDeploymentGateEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/release-receipts/*",
    ]
  }

  # PR2-A0.2: supply-chain adopt の integrity 検査（HeadObject + VersionId 固定
  # GetObject + Object Lock メタデータ）と、adopt(import) 時の provider read
  # （HeadObject + GetObjectTagging）に必要な最小 read だけを許可する。
  # GetObjectRetention は 403 を返す権限ではなく、無いと HeadObject の応答から
  # lock mode / retain-until ヘッダが黙って欠落する種類の権限（integrity は
  # None 検出で fail-closed する）。GetObjectVersion は既に別 statement で許可済み。
  # 書き込みは boundary / control-plane / bucket policy の Deny 群がそのまま塞ぐ。
  # prefix scope なのは、世代 key（content-addressed sha）が manifest refresh の
  # たびに変わるため。対象は GOVERNANCE ロック済みの不変オブジェクトのみ。
  statement {
    sid = "ReadExactSupplyChainBuildspecGenerations"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectTagging",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/codebuild-buildspecs/${local.mcp_source_publisher_project_name}/*",
      "${aws_s3_bucket.image_release_evidence.arn}/codebuild-buildspecs/${local.image_attestor_project_name}/*",
      "${aws_s3_bucket.image_release_evidence.arn}/codebuild-buildspecs/${local.image_promoter_project_name}/*",
      "${aws_s3_bucket.image_release_evidence.arn}/codebuild-buildspecs/${local.approval_publisher_project_name}/*",
    ]
  }

  # PR2-A0.2.2a: guarded plan の refresh が aws_s3_bucket を読むときに provider
  # （terraform-provider-aws 5.100.0）が呼ぶ bucket 設定 read。
  #
  # 由来（推測での追加は禁止。2026-08-20T05:45Z の automation role による refresh の
  # CloudTrail 全件と simulate-principal-policy の実測に基づく）:
  #   403 実測  … s3:GetBucketCORS（exact 6 bucket で AccessDenied）
  #   同経路継続 … 残り 6 action。CORS で refresh が abort したため 403 は観測できて
  #                いないが、(a) simulate-principal-policy が全 bucket で
  #                implicitDeny を返すこと（= identity policy に無い）と
  #                (b) bootstrap seed-stack の ReadExactTerraformBuckets が同じ
  #                provider 版の aws_s3_bucket read に対してこの 7 action 集合を
  #                exact bucket ARN で付与済みであること、の 2 点で必要と判定した。
  #                CORS だけ足すと次の refresh で同じ壁を 6 回踏む。
  # なお s3:GetBucketVersioning 等は既に別 statement で許可済み（simulate=allowed）。
  # 誤 action 名 s3:GetBucketLifecycleConfiguration は使わない（正しくは
  # s3:GetLifecycleConfiguration。bootstrap 側は commit 722a94a で訂正済み）。
  # bucket-level の read なので resource に /* は付けない。count 条件付き bucket は
  # ReadExactDeploymentSubjectGraph と同じ形で条件付き concat() にし、無効時は付与しない。
  #
  # ARN は **bucket resource ではなく bucket 名の式から組み立てる**。bucket resource を
  # 参照すると bootstrap の reviewed closure（tests/bootstrap/..._closure.py）へ
  # managed resource が 4 件流入し、bootstrap 供給契約（infra/bootstrap/
  # bootstrap_contract.json の existing_dependency_addresses 等）まで書き換えが波及する。
  # read-only の grant のためにその契約を広げるのは activation の scope 外なので、
  # 各 bucket resource が bucket 名に使っているのと同一の式を再利用する
  # （名前がずれれば下のカバレッジテストが検出する）。
  statement {
    sid = "ReadExactTerraformBucketConfigurations"
    actions = [
      "s3:GetAccelerateConfiguration",
      "s3:GetBucketCORS",
      "s3:GetBucketLogging",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketWebsite",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
    ]
    resources = concat(
      [
        "arn:aws:s3:::${var.project_name}-${var.environment}-raw-files",
        "arn:aws:s3:::${local.release_evidence_bucket}",
        "arn:aws:s3:::${local.openclaw_evidence_bucket}",
        "arn:aws:s3:::${local.openclaw_rollout_evidence_bucket}",
      ],
      var.enable_cloudtrail ? [
        "arn:aws:s3:::${var.project_name}-${var.environment}-cloudtrail-${data.aws_caller_identity.current.account_id}",
      ] : [],
      var.enable_bedrock_invocation_logging ? [
        "arn:aws:s3:::${var.project_name}-${var.environment}-bedrock-logs-${data.aws_caller_identity.current.account_id}",
      ] : [],
      local.tk_enabled == 1 ? ["arn:aws:s3:::${local.media_bucket_name}"] : [],
    )
  }

  # PR2-A0.2.2a: bastion（t4g.nano）/ worker（t4g.medium）の EC2 インスタンスに対する
  # provider read が呼ぶ。2026-08-20 の refresh で 2 回 AccessDenied を実測。
  # DescribeInstanceTypes は instance type を ARN で表現しないため resource は "*"
  # のみ（repo 内の他の ec2 Describe 系 statement も同じく "*" scope）。read-only で、
  # 書き込みは boundary / control-plane の Deny 群がそのまま塞ぐ。
  # ※コメントに Terraform リソースアドレスを書かないこと: bootstrap closure テストが
  #   コメント本文も参照として走査するため、偽のグラフ辺が入る（2026-08-24 実測）。
  statement {
    sid       = "ReadExactInstanceTypeCatalog"
    actions   = ["ec2:DescribeInstanceTypes"]
    resources = ["*"]
  }


  # PR2-A0.2.2b: rds.tf の db_password secret version（managed resource）に対する
  # provider read。secret の **値** を読む唯一の箇所で、2026-08-20T05:45:10Z の refresh で
  # AccessDenied を実測している。A0.2.2a（non-secret read）とは意図的に別 PR / 別
  # statement に分けている（secret 面の境界判断を独立にレビューするため）。
  #
  # scope: db_password の exact ARN 1 本のみ。6 桁 suffix を含む完全形で、
  # ワイルドカード（-* / -??????）は使わない。guard 側の validate_exact_runtime_iam_plan
  # も secretsmanager: を Allow する statement へ exact secret ARN を要求する。
  #
  # kms:Decrypt は **追加しない**。実測 2 点で不要と確定:
  #   1. この secret の KmsKeyId は未設定 = AWS managed key
  #      （alias/aws/secretsmanager = f512e1ea…, KeyManager=AWS）。その key policy が
  #      Principal {"AWS":"*"} + kms:ViaService=secretsmanager.ap-northeast-1 +
  #      kms:CallerAccount 条件で Decrypt を直接許可している（root 委譲ではない）
  #   2. 対照実験: teamagent-dev-ecs-exec-mcp は同じ AWS managed key で暗号化された
  #      teamagent/dev/database-url に対し GetSecretValue=allowed / kms:Decrypt=
  #      implicitDeny のまま本番稼働している
  # customer managed key へ移行した場合は exact key ARN で別途レビューすること。
  #
  # 取り扱い: この許可により plan / show -json が DB パスワードを内包し得る。
  # 成果物は repo 外 0600・生 JSON をチャット / CI ログへ出さない・機械検証と
  # redacted 要約のみ人間へ・作業後に破棄（docs/runbooks/secret_bearing_plan.md）。
  # ARN は secret resource を参照せず literal で書く。理由 2 点:
  #   1. Secrets Manager の 6 桁 suffix は AWS 生成で config から導出できない。guard の
  #      exact_secret_arn は -[A-Za-z0-9]{6}$ を要求するため、ワイルドカードは契約違反
  #   2. secret resource を参照すると bootstrap の reviewed closure へ managed resource が
  #      流入し、bootstrap 供給契約まで書き換えが波及する（read grant のために広げない）
  # secret を作り直すと suffix が変わり refresh が 403 で fail-closed する（検出可能）。
  statement {
    sid     = "ReadExactTerraformManagedSecretValue"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${var.project_name}/${var.environment}/db_password-ObO75F",
    ]
  }

  statement {
    sid       = "DecryptExactDeploymentGateEvidence"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }

  statement {
    sid       = "VerifyExactDeploymentGateEvidence"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.image_attestor_signing.arn]
  }

  statement {
    sid = "ReadExactDeploymentSubjectGraph"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetLifecyclePolicy",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp.arn,
        aws_ecr_repository.mcp_media.arn,
        aws_ecr_repository.openclaw.arn,
        aws_ecr_repository.openclaw_media.arn,
      ],
      local.tk_enabled == 1 ? [aws_ecr_repository.tiktok_acquire[0].arn] : [],
    )
  }

  statement {
    sid = "TransitionExactDeploymentIntentLedger"
    actions = [
      "dynamodb:DeleteItem",
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
        "receipt#*",
      ]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }
}

resource "aws_iam_role_policy" "runtime_evidence_automation" {
  name   = "${local.runtime_automation_role_name}-evidence"
  role   = aws_iam_role.runtime_automation.id
  policy = data.aws_iam_policy_document.runtime_evidence_automation.json
}

data "aws_iam_policy_document" "runtime_automation_control_plane_manage_a" {
  statement {
    sid = "ManageExactTerraformResourceTypes"
    actions = [
      "access-analyzer:CreateAnalyzer",
      "access-analyzer:DeleteAnalyzer",
      "access-analyzer:GetAnalyzer",
      "access-analyzer:ListTagsForResource",
      "access-analyzer:TagResource",
      "access-analyzer:UntagResource",
      "access-analyzer:UpdateAnalyzer",
      "apigateway:DELETE",
      "apigateway:GET",
      "apigateway:PATCH",
      "apigateway:POST",
      "apigateway:PUT",
      "bedrock:DeleteModelInvocationLoggingConfiguration",
      "bedrock:GetModelInvocationLoggingConfiguration",
      "bedrock:PutModelInvocationLoggingConfiguration",
      "aws-portal:ModifyBilling",
      "aws-portal:ViewBilling",
      "budgets:ListTagsForResource",
      "budgets:ModifyBudget",
      "budgets:TagResource",
      "budgets:UntagResource",
      "budgets:ViewBudget",
      "ce:CreateAnomalyMonitor",
      "ce:CreateAnomalySubscription",
      "ce:DeleteAnomalyMonitor",
      "ce:DeleteAnomalySubscription",
      "ce:GetAnomalyMonitors",
      "ce:GetAnomalySubscriptions",
      "ce:ListTagsForResource",
      "ce:TagResource",
      "ce:UntagResource",
      "ce:UpdateAnomalyMonitor",
      "ce:UpdateAnomalySubscription",
      "cloudtrail:AddTags",
      "cloudtrail:CreateTrail",
      "cloudtrail:DeleteTrail",
      "cloudtrail:GetEventSelectors",
      "cloudtrail:GetTrail",
      "cloudtrail:GetTrailStatus",
      "cloudtrail:ListTags",
      "cloudtrail:PutEventSelectors",
      "cloudtrail:RemoveTags",
      "cloudtrail:StartLogging",
      "cloudtrail:StopLogging",
      "cloudtrail:UpdateTrail",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:DeleteDashboards",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:GetDashboard",
      "cloudwatch:ListDashboards",
      "cloudwatch:ListTagsForResource",
      "cloudwatch:PutDashboard",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
      "codebuild:BatchGetProjects",
      "codebuild:CreateProject",
      "codebuild:DeleteProject",
      "codebuild:ListTagsForResource",
      "codebuild:TagResource",
      "codebuild:UntagResource",
      "codebuild:UpdateProject",
      "codeconnections:CreateConnection",
      "codeconnections:DeleteConnection",
      "codeconnections:GetConnection",
      "codeconnections:ListConnections",
      "codeconnections:ListTagsForResource",
      "codeconnections:TagResource",
      "codeconnections:UntagResource",
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeContinuousBackups",
      "dynamodb:DescribeTable",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTimeToLive",
      "ec2:AssociateIamInstanceProfile",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:CreateSecurityGroup",
      "ec2:CreateTags",
      "ec2:CreateVpcEndpoint",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteTags",
      "ec2:DeleteVpcEndpoints",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeIamInstanceProfileAssociations",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribePrefixLists",
      "ec2:DescribeRouteTables",
      "ec2:DescribeSecurityGroupRules",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
      "ec2:DescribeTags",
      "ec2:DescribeVpcAttribute",
      "ec2:DescribeVpcEndpoints",
      "ec2:DescribeVpcs",
      "ec2:DisassociateIamInstanceProfile",
      "ec2:ModifyInstanceAttribute",
      "ec2:ModifyVpcEndpoint",
      "ec2:ReplaceIamInstanceProfileAssociation",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ecr:CreateRepository",
      "ecr:DeleteLifecyclePolicy",
      "ecr:DeleteRepository",
      "ecr:DeleteRepositoryPolicy",
      "ecr:DescribeRepositories",
      "ecr:GetLifecyclePolicy",
      "ecr:GetRepositoryPolicy",
      "ecr:ListTagsForResource",
      "ecr:PutImageScanningConfiguration",
      "ecr:PutImageTagMutability",
      "ecr:PutLifecyclePolicy",
      "ecr:SetRepositoryPolicy",
      "ecr:TagResource",
      "ecr:UntagResource",
      "ecs:CreateCluster",
      "ecs:CreateService",
      "ecs:DeleteCluster",
      "ecs:DeleteService",
      "ecs:DeregisterTaskDefinition",
      "ecs:DescribeClusters",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:ListTagsForResource",
      "ecs:PutClusterCapacityProviders",
      "ecs:RegisterTaskDefinition",
      "ecs:TagResource",
      "ecs:UntagResource",
      "ecs:UpdateClusterSettings",
      "ecs:UpdateService",
      "elasticfilesystem:CreateAccessPoint",
      "elasticfilesystem:CreateFileSystem",
      "elasticfilesystem:CreateMountTarget",
      "elasticfilesystem:DeleteAccessPoint",
      "elasticfilesystem:DeleteFileSystem",
      "elasticfilesystem:DeleteMountTarget",
      "elasticfilesystem:DescribeAccessPoints",
      "elasticfilesystem:DescribeFileSystems",
      "elasticfilesystem:DescribeLifecycleConfiguration",
      "elasticfilesystem:DescribeMountTargets",
      "elasticfilesystem:DescribeMountTargetSecurityGroups",
      "elasticfilesystem:DescribeTags",
      "elasticfilesystem:ListTagsForResource",
      "elasticfilesystem:ModifyMountTargetSecurityGroups",
      "elasticfilesystem:PutLifecycleConfiguration",
      "elasticfilesystem:TagResource",
      "elasticfilesystem:UntagResource",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "runtime_automation_control_plane_manage_b" {
  statement {
    sid = "ManageExactTerraformResourceTypes"
    actions = [
      "elasticloadbalancing:AddTags",
      "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DeregisterTargets",
      "elasticloadbalancing:DescribeTags",
      "elasticloadbalancing:DescribeTargetGroupAttributes",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
      "elasticloadbalancing:ModifyTargetGroup",
      "elasticloadbalancing:ModifyTargetGroupAttributes",
      "elasticloadbalancing:RegisterTargets",
      "elasticloadbalancing:RemoveTags",
      "events:DeleteRule",
      "events:DescribeRule",
      "events:DisableRule",
      "events:EnableRule",
      "events:ListTagsForResource",
      "events:ListTargetsByRule",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "events:TagResource",
      "events:UntagResource",
      "kms:CancelKeyDeletion",
      "kms:CreateAlias",
      "kms:CreateKey",
      "kms:DeleteAlias",
      "kms:DescribeKey",
      "kms:EnableKeyRotation",
      "kms:GetKeyPolicy",
      "kms:GetKeyRotationStatus",
      "kms:ListAliases",
      "kms:ListResourceTags",
      "kms:PutKeyPolicy",
      "kms:ScheduleKeyDeletion",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateAlias",
      "lambda:AddPermission",
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:GetFunction",
      "lambda:GetFunctionCodeSigningConfig",
      "lambda:GetFunctionConfiguration",
      "lambda:GetPolicy",
      "lambda:ListEventSourceMappings",
      "lambda:ListFunctionEventInvokeConfigs",
      "lambda:ListTags",
      "lambda:ListVersionsByFunction",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
      "lambda:UpdateEventSourceMapping",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:DeleteMetricFilter",
      "logs:DeleteRetentionPolicy",
      "logs:DescribeLogGroups",
      "logs:DescribeMetricFilters",
      "logs:ListTagsForResource",
      "logs:PutMetricFilter",
      "logs:PutRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "rds:AddTagsToResource",
      "rds:CreateDBInstance",
      "rds:CreateDBParameterGroup",
      "rds:CreateDBSubnetGroup",
      "rds:DeleteDBInstance",
      "rds:DeleteDBParameterGroup",
      "rds:DeleteDBSubnetGroup",
      "rds:DescribeDBInstances",
      "rds:DescribeDBParameters",
      "rds:DescribeDBParameterGroups",
      "rds:DescribeDBSubnetGroups",
      "rds:ListTagsForResource",
      "rds:ModifyDBInstance",
      "rds:ModifyDBParameterGroup",
      "rds:ModifyDBSubnetGroup",
      "rds:RemoveTagsFromResource",
      "rds:ResetDBParameterGroup",
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:GetBucketAcl",
      "s3:GetBucketLifecycleConfiguration",
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetEncryptionConfiguration",
      "s3:ListBucket",
      "s3:PutBucketLifecycleConfiguration",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutBucketPolicy",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketTagging",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "scheduler:CreateSchedule",
      "scheduler:CreateScheduleGroup",
      "scheduler:DeleteSchedule",
      "scheduler:DeleteScheduleGroup",
      "scheduler:GetSchedule",
      "scheduler:GetScheduleGroup",
      "scheduler:ListTagsForResource",
      "scheduler:TagResource",
      "scheduler:UntagResource",
      "scheduler:UpdateSchedule",
      "secretsmanager:CreateSecret",
      "secretsmanager:DeleteResourcePolicy",
      "secretsmanager:DeleteSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:ListSecretVersionIds",
      "secretsmanager:ListSecrets",
      "secretsmanager:PutResourcePolicy",
      "secretsmanager:PutSecretValue",
      "secretsmanager:RestoreSecret",
      "secretsmanager:TagResource",
      "secretsmanager:UntagResource",
      "secretsmanager:UpdateSecret",
      "servicediscovery:CreatePrivateDnsNamespace",
      "servicediscovery:CreateService",
      "servicediscovery:DeleteNamespace",
      "servicediscovery:DeleteService",
      "servicediscovery:GetNamespace",
      "servicediscovery:GetOperation",
      "servicediscovery:GetService",
      "servicediscovery:ListTagsForResource",
      "servicediscovery:TagResource",
      "servicediscovery:UntagResource",
      "servicediscovery:UpdateService",
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTagsForResource",
      "sns:SetTopicAttributes",
      "sns:TagResource",
      "sns:UntagResource",
      "sqs:CreateQueue",
      "sqs:DeleteQueue",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ListQueueTags",
      "sqs:SetQueueAttributes",
      "sqs:TagQueue",
      "sqs:UntagQueue",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "runtime_automation_control_plane_core" {
  statement {
    sid = "ReadExactIamMetadata"
    actions = [
      "iam:GetInstanceProfile",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:GetUser",
      "iam:GetUserPolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListPolicyTags",
      "iam:ListPolicyVersions",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:ListUserPolicies",
      "iam:ListUserTags",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "PassOnlyExistingTeamAgentServiceRoles"
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::718959508629:role/teamagent-dev-bastion",
      "arn:aws:iam::718959508629:role/teamagent-dev-canary-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-connect-web-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-canary",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-connect-web",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-ingest",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-mcp",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-morning-digest",
      "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-openclaw",
      "arn:aws:iam::718959508629:role/teamagent-dev-events-canary-invoke",
      "arn:aws:iam::718959508629:role/teamagent-dev-events-ingest-invoke",
      "arn:aws:iam::718959508629:role/teamagent-dev-events-morning-digest-invoke",
      "arn:aws:iam::718959508629:role/teamagent-dev-ingest-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-lambda-exec",
      "arn:aws:iam::718959508629:role/teamagent-dev-mcp-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-morning-digest-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-openclaw-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-reminder-notify",
      "arn:aws:iam::718959508629:role/teamagent-dev-reminder-scheduler",
      "arn:aws:iam::718959508629:role/teamagent-dev-tiktok-acquire-dispatch",
      "arn:aws:iam::718959508629:role/teamagent-dev-tiktok-acquire-exec",
      "arn:aws:iam::718959508629:role/teamagent-dev-tiktok-acquire-janitor",
      "arn:aws:iam::718959508629:role/teamagent-dev-tiktok-acquire-task",
      "arn:aws:iam::718959508629:role/teamagent-dev-worker",
      "arn:aws:iam::718959508629:role/teamagent-dev-x-buzz-dispatch",
      "arn:aws:iam::718959508629:role/teamagent-dev-x-buzz-exec",
      "arn:aws:iam::718959508629:role/teamagent-dev-x-buzz-task",
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values = [
        "ec2.amazonaws.com",
        "ecs-tasks.amazonaws.com",
        "events.amazonaws.com",
        "lambda.amazonaws.com",
        "scheduler.amazonaws.com",
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
    sid    = "DenyImageAndKeyMutation"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "kms:GenerateMac",
      "kms:ScheduleKeyDeletion",
    ]
    resources = ["*"]
  }

  statement {
    sid           = "DenyNonRolloutSigning"
    effect        = "Deny"
    actions       = ["kms:Sign"]
    not_resources = [aws_kms_key.openclaw_rollout_signing.arn]
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

resource "aws_iam_policy" "runtime_automation_control_plane_manage_a" {
  name   = "${local.runtime_automation_role_name}-control-plane-manage-a"
  policy = data.aws_iam_policy_document.runtime_automation_control_plane_manage_a.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.runtime_automation_control_plane_manage_a.json, "/\\s/", "")) < 6144
      error_message = "Runtime automation control-plane manage-a policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_policy" "runtime_automation_control_plane_manage_b" {
  name   = "${local.runtime_automation_role_name}-control-plane-manage-b"
  policy = data.aws_iam_policy_document.runtime_automation_control_plane_manage_b.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.runtime_automation_control_plane_manage_b.json, "/\\s/", "")) < 6144
      error_message = "Runtime automation control-plane manage-b policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_policy" "runtime_automation_control_plane_core" {
  name   = "${local.runtime_automation_role_name}-control-plane-core"
  policy = data.aws_iam_policy_document.runtime_automation_control_plane_core.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.runtime_automation_control_plane_core.json, "/\\s/", "")) < 6144
      error_message = "Runtime automation control-plane core policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_role_policy_attachment" "runtime_automation_control_plane_manage_a" {
  role       = aws_iam_role.runtime_automation.name
  policy_arn = aws_iam_policy.runtime_automation_control_plane_manage_a.arn
}

resource "aws_iam_role_policy_attachment" "runtime_automation_control_plane_manage_b" {
  role       = aws_iam_role.runtime_automation.name
  policy_arn = aws_iam_policy.runtime_automation_control_plane_manage_b.arn
}

resource "aws_iam_role_policy_attachment" "runtime_automation_control_plane_core" {
  role       = aws_iam_role.runtime_automation.name
  policy_arn = aws_iam_policy.runtime_automation_control_plane_core.arn
}

check "runtime_evidence_owned_preconditions" {
  assert {
    condition = (
      aws_kms_key.alarm_recipient_ack.key_usage == "SIGN_VERIFY" &&
      aws_kms_key.alarm_recipient_ack.customer_master_key_spec == "ECC_NIST_P256" &&
      aws_iam_role.alarm_recipient_ack_signer.arn ==
      "arn:aws:iam::718959508629:role/teamagent-dev-alarm-recipient-ack-signer" &&
      aws_iam_role.runtime_automation.arn ==
      local.runtime_automation_role_arn &&
      aws_iam_role.runtime_automation.permissions_boundary ==
      aws_iam_policy.runtime_automation_boundary.arn
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

output "runtime_automation_boundary_arn" {
  value = aws_iam_policy.runtime_automation_boundary.arn
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

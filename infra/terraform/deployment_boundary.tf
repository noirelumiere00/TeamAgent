# ============================================================
# Human direct-mutation boundary
# ============================================================
# The guarded workflow needs read-only AWS access plus temporary preflight
# RunTask/RegisterTaskDefinition access, so those calls remain available to the
# operator. Promotion calls that can attach a task definition to production,
# mutate schedules, replace dispatchers, or launch the retired builder are
# denied on the long-lived AIIAdev identity. A reviewed deployment automation
# role (owned outside this state) must perform a saved-plan apply.
#
# AWS account root is not governed by IAM identity policies. The legacy builder
# is therefore independently made non-publishing in codebuild.tf; preventing
# root from changing IAM again requires an Organizations SCP and root credential
# controls outside this Terraform state.

data "aws_iam_user" "runtime_operator" {
  user_name = "AIIAdev"
}

locals {
  runtime_direct_deny_policy_name = (
    "${var.project_name}-${var.environment}-deny-direct-runtime-mutation"
  )
  runtime_direct_deny_policy_arn = (
    "arn:aws:iam::${local.account_id}:policy/${local.runtime_direct_deny_policy_name}"
  )
  runtime_service_arns = [
    for service in ["mcp", "connect-web", "openclaw"] :
    "arn:aws:ecs:${var.aws_region}:${local.account_id}:service/${var.project_name}-${var.environment}/${var.project_name}-${var.environment}-${service}"
  ]
  runtime_rule_arns = [
    "arn:aws:events:${var.aws_region}:${local.account_id}:rule/${var.project_name}-${var.environment}-ingest-weekly",
    "arn:aws:events:${var.aws_region}:${local.account_id}:rule/${var.project_name}-${var.environment}-morning-digest-weekday",
    "arn:aws:events:${var.aws_region}:${local.account_id}:rule/${var.project_name}-${var.environment}-canary-hourly",
  ]
  runtime_dispatcher_arns = [
    "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${var.project_name}-${var.environment}-tiktok-acquire-dispatch",
    "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${var.project_name}-${var.environment}-x-buzz-dispatch",
  ]
  # These ARN patterns remain inside the single TeamAgent dev account/region.
  # Event-source mapping ARNs contain only a UUID, so all mappings owned by this
  # project operator are denied; reviewed deployment automation remains
  # unaffected because this policy is attached only to AIIAdev.
  runtime_guarded_control_plane_arns = [
    "arn:aws:cloudwatch:${var.aws_region}:${local.account_id}:alarm:${var.project_name}-${var.environment}-*",
    "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-${var.environment}-*",
    "arn:aws:ecs:${var.aws_region}:${local.account_id}:task-definition/${var.project_name}-${var.environment}-*:*",
    "arn:aws:iam::${local.account_id}:role/${var.project_name}-${var.environment}-*",
    "arn:aws:lambda:${var.aws_region}:${local.account_id}:event-source-mapping:*",
    "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/apigateway/${var.project_name}-${var.environment}-*:*",
    "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${var.project_name}-${var.environment}-*:*",
    "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/${var.project_name}/${var.environment}/*:*",
    "arn:aws:sns:${var.aws_region}:${local.account_id}:${var.project_name}-${var.environment}-openclaw-alarms",
    "arn:aws:sns:${var.aws_region}:${local.account_id}:${var.project_name}-${var.environment}-openclaw-alarms:*",
    "arn:aws:sqs:${var.aws_region}:${local.account_id}:${var.project_name}-${var.environment}-*",
  ]
}

data "aws_iam_policy_document" "runtime_direct_mutation_deny" {
  statement {
    sid    = "DenyDirectServicePromotion"
    effect = "Deny"
    actions = [
      "ecs:CreateService",
      "ecs:DeleteService",
      "ecs:UpdateService",
    ]
    resources = local.runtime_service_arns
  }

  statement {
    sid    = "DenyDirectScheduleMutation"
    effect = "Deny"
    actions = [
      "events:DeleteRule",
      "events:DisableRule",
      "events:EnableRule",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
    ]
    resources = local.runtime_rule_arns
  }

  statement {
    sid    = "DenyDirectDispatcherMutation"
    effect = "Deny"
    actions = [
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:PublishVersion",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = local.runtime_dispatcher_arns
  }

  statement {
    sid    = "DenyRetiredAndUnreviewedImageBuilds"
    effect = "Deny"
    actions = [
      "codebuild:BatchDeleteBuilds",
      "codebuild:DeleteProject",
      "codebuild:RetryBuild",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:UpdateProject",
    ]
    resources = [
      "arn:aws:codebuild:${var.aws_region}:${local.account_id}:project/${var.project_name}-${var.environment}-*",
    ]
  }

  statement {
    sid    = "DenyExecuteApiEndpointReenable"
    effect = "Deny"
    actions = [
      "apigateway:DELETE",
      "apigateway:PATCH",
      "apigateway:POST",
      "apigateway:PUT",
    ]
    resources = [
      "arn:aws:apigateway:${var.aws_region}::/apis/esk97z9grh",
      "arn:aws:apigateway:${var.aws_region}::/apis/esk97z9grh/*",
    ]
  }

  # A targeted Terraform invocation can otherwise omit terraform_data.runtime_guard
  # for foundational resources which cannot depend on the guard without forming
  # a graph cycle. Deny their mutating control-plane APIs on the long-lived human
  # identity after bootstrap. RegisterTaskDefinition/RunTask/PassRole and
  # DeregisterTaskDefinition remain available because the real Fargate preflight
  # must create and clean up temporary, non-production task revisions; production
  # promotion and permanent task-definition deletion remain denied separately.
  statement {
    sid    = "DenyDirectGuardedControlPlaneMutation"
    effect = "Deny"
    actions = [
      "cloudwatch:DeleteAlarms",
      "cloudwatch:DisableAlarmActions",
      "cloudwatch:EnableAlarmActions",
      "cloudwatch:PutCompositeAlarm",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:SetAlarmState",
      "dynamodb:DeleteTable",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTimeToLive",
      "ecs:DeleteTaskDefinitions",
      "iam:AttachRolePolicy",
      "iam:DeleteRole",
      "iam:DeleteRolePermissionsBoundary",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePermissionsBoundary",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "lambda:DeleteEventSourceMapping",
      "lambda:UpdateEventSourceMapping",
      "logs:DeleteLogGroup",
      "logs:DeleteRetentionPolicy",
      "logs:PutRetentionPolicy",
      "sns:AddPermission",
      "sns:ConfirmSubscription",
      "sns:DeleteTopic",
      "sns:RemovePermission",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sqs:AddPermission",
      "sqs:DeleteQueue",
      "sqs:PurgeQueue",
      "sqs:RemovePermission",
      "sqs:SetQueueAttributes",
    ]
    resources = local.runtime_guarded_control_plane_arns
  }

  # CreateEventSourceMapping has no resource ARN at authorization time, but it
  # exposes lambda:FunctionArn. This condition blocks duplicate consumers only
  # for the two guarded dispatchers.
  statement {
    sid       = "DenyDirectDispatcherMappingCreation"
    effect    = "Deny"
    actions   = ["lambda:CreateEventSourceMapping"]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "lambda:FunctionArn"
      values   = local.runtime_dispatcher_arns
    }
  }

  # SNS does not support resource-level authorization for these two actions.
  # This project-specific operator must not be able to silence any confirmed
  # subscription after the reviewed bootstrap has attached this policy.
  statement {
    sid       = "DenyDirectSubscriptionRemoval"
    effect    = "Deny"
    actions   = ["sns:SetSubscriptionAttributes", "sns:Unsubscribe"]
    resources = ["*"]
  }

  statement {
    sid    = "DenyBoundaryDetach"
    effect = "Deny"
    actions = [
      "iam:DetachUserPolicy",
    ]
    resources = [data.aws_iam_user.runtime_operator.arn]

    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [local.runtime_direct_deny_policy_arn]
    }
  }

  statement {
    sid    = "DenyBoundaryPolicyMutation"
    effect = "Deny"
    actions = [
      "iam:CreatePolicyVersion",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:SetDefaultPolicyVersion",
    ]
    resources = [local.runtime_direct_deny_policy_arn]
  }
}

resource "aws_iam_policy" "runtime_direct_mutation_deny" {
  name        = local.runtime_direct_deny_policy_name
  description = "Fail-closed deny for direct TeamAgent production runtime mutation"
  policy      = data.aws_iam_policy_document.runtime_direct_mutation_deny.json

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_user_policy_attachment" "runtime_direct_mutation_deny" {
  user       = data.aws_iam_user.runtime_operator.user_name
  policy_arn = aws_iam_policy.runtime_direct_mutation_deny.arn

  # Attach only after the one-time migration's production resources and retired
  # builder have converged. This avoids revoking the current operator midway
  # through a reviewed bootstrap apply.
  depends_on = [
    aws_apigatewayv2_api.connect_web,
    aws_codebuild_project.image,
    aws_ecs_service.connect_web,
    aws_ecs_service.mcp,
    aws_ecs_service.openclaw,
    aws_lambda_function.tiktok_dispatch,
    aws_lambda_function.x_dispatch,
    aws_cloudwatch_event_target.canary_run_task,
    aws_cloudwatch_event_target.ingest_run_task,
    aws_cloudwatch_event_target.morning_digest_run_task,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

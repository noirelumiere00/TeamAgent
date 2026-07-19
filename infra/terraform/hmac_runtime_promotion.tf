# Apply-time HMAC runtime mutation gates. Every ECS service/EventBridge target change is owned by
# the same complete saved plan whose production release intent is consumed under the shared lock.

locals {
  hmac_promotion_gate_environment = {
    TEAMAGENT_HMAC_PROMOTION_FROM_TERRAFORM = "1"
    HMAC_GATE_ENABLED                       = "true"
    HMAC_GATE_PYTHON                        = var.hmac_gate_python
    HMAC_GATE_SCRIPT                        = abspath("${path.module}/../../scripts/terraform_hmac_promotion_gate.py")
    HMAC_GATE_MODE                          = var.hmac_gate_mode
    HMAC_CLEANUP_DOMAIN                     = var.hmac_cleanup_domain
    HMAC_PREFLIGHT_MANIFEST                 = var.hmac_live_manifest_path
    HMAC_ROLLOUT_CONTROL                    = var.hmac_rollout_control_path
  }
  hmac_candidate_task_definition_arns = {
    mcp            = try(aws_ecs_task_definition.mcp.arn, "")
    connect_web    = try(aws_ecs_task_definition.connect_web[0].arn, "")
    morning_digest = try(aws_ecs_task_definition.morning_digest[0].arn, "")
  }
  hmac_promoted_task_definition_arns = {
    for task, candidate_arn in local.hmac_candidate_task_definition_arns :
    task => var.hmac_gate_mode == "rollback"
    ? local.hmac_rollback_task_definition_arns[task]
    : candidate_arn
  }
  hmac_morning_digest_target = {
    Id      = "morning"
    Arn     = aws_ecs_cluster.main.arn
    RoleArn = try(aws_iam_role.events_morning_digest_invoke[0].arn, "")
    Input   = jsonencode({})
    EcsParameters = {
      TaskDefinitionArn = local.hmac_promoted_task_definition_arns.morning_digest
      TaskCount         = 1
      LaunchType        = "FARGATE"
      PlatformVersion   = "LATEST"
      NetworkConfiguration = {
        awsvpcConfiguration = {
          Subnets        = data.aws_subnets.default.ids
          SecurityGroups = try([aws_security_group.morning_digest[0].id], [])
          AssignPublicIp = "ENABLED"
        }
      }
    }
    RetryPolicy = {
      MaximumEventAgeInSeconds = 3600
      MaximumRetryAttempts     = 1
    }
  }
}

resource "terraform_data" "hmac_connect_web_pre_update" {
  count = local.hmac_live_gate_enabled.connect_web && var.enable_connect_web && var.mcp_image != "" && contains(var.hmac_runtime_promotion_tasks, "connect_web") ? 1 : 0

  triggers_replace = [
    aws_ecs_task_definition.connect_web[0].arn,
    var.hmac_gate_mode,
    filesha256(var.hmac_live_manifest_path),
    filesha256(var.hmac_rollout_control_path),
  ]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = merge(local.hmac_promotion_gate_environment, {
      HMAC_GATE_ACTION         = "pre-update"
      HMAC_GATE_TASK           = "connect_web"
      HMAC_REGISTERED_TASK_ARN = local.hmac_promoted_task_definition_arns.connect_web
    })
  }

  depends_on = [
    terraform_data.production_image_release_gate,
    aws_ecs_task_definition.connect_web,
  ]
}

resource "terraform_data" "hmac_connect_web_post_update" {
  count = local.hmac_live_gate_enabled.connect_web && var.enable_connect_web && var.mcp_image != "" && contains(var.hmac_runtime_promotion_tasks, "connect_web") ? 1 : 0

  triggers_replace = [terraform_data.hmac_connect_web_pre_update[0].id]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = merge(local.hmac_promotion_gate_environment, {
      HMAC_GATE_ACTION         = "post-update"
      HMAC_GATE_TASK           = "connect_web"
      HMAC_REGISTERED_TASK_ARN = local.hmac_promoted_task_definition_arns.connect_web
    })
  }

  depends_on = [aws_ecs_service.connect_web]
}

resource "terraform_data" "hmac_mcp_pre_update" {
  count = local.hmac_live_gate_enabled.mcp && var.mcp_image != "" && contains(var.hmac_runtime_promotion_tasks, "mcp") ? 1 : 0

  triggers_replace = [
    aws_ecs_task_definition.mcp.arn,
    var.hmac_gate_mode,
    filesha256(var.hmac_live_manifest_path),
    filesha256(var.hmac_rollout_control_path),
  ]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = merge(local.hmac_promotion_gate_environment, {
      HMAC_GATE_ACTION         = "pre-update"
      HMAC_GATE_TASK           = "mcp"
      HMAC_REGISTERED_TASK_ARN = local.hmac_promoted_task_definition_arns.mcp
    })
  }

  depends_on = [
    terraform_data.production_image_release_gate,
    aws_ecs_task_definition.mcp,
  ]
}

resource "terraform_data" "hmac_mcp_post_update" {
  count = local.hmac_live_gate_enabled.mcp && var.mcp_image != "" && contains(var.hmac_runtime_promotion_tasks, "mcp") ? 1 : 0

  triggers_replace = [terraform_data.hmac_mcp_pre_update[0].id]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = merge(local.hmac_promotion_gate_environment, {
      HMAC_GATE_ACTION         = "post-update"
      HMAC_GATE_TASK           = "mcp"
      HMAC_REGISTERED_TASK_ARN = local.hmac_promoted_task_definition_arns.mcp
    })
  }

  depends_on = [aws_ecs_service.mcp]
}

resource "terraform_data" "hmac_morning_digest_target_transaction" {
  count = local.hmac_live_gate_enabled.morning_digest && var.enable_morning_digest && var.mcp_image != "" && contains(var.hmac_runtime_promotion_tasks, "morning_digest") ? 1 : 0

  input = {
    mode                   = var.hmac_gate_mode
    expected_rule_state    = "DISABLED"
    target                 = local.hmac_morning_digest_target
    task_definition_arn    = local.hmac_promoted_task_definition_arns.morning_digest
    manifest_sha256        = filesha256(var.hmac_live_manifest_path)
    rollout_control_sha256 = filesha256(var.hmac_rollout_control_path)
  }

  triggers_replace = [
    timestamp(),
    local.hmac_promoted_task_definition_arns.morning_digest,
    sha256(jsonencode(local.hmac_morning_digest_target)),
    var.hmac_gate_mode,
    filesha256(var.hmac_live_manifest_path),
    filesha256(var.hmac_rollout_control_path),
  ]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = merge(local.hmac_promotion_gate_environment, {
      HMAC_GATE_ACTION         = "event-transaction"
      HMAC_GATE_TASK           = "morning_digest"
      HMAC_REGISTERED_TASK_ARN = local.hmac_promoted_task_definition_arns.morning_digest
      HMAC_EVENT_TARGET_JSON   = jsonencode(local.hmac_morning_digest_target)
    })
  }

  depends_on = [
    terraform_data.production_image_release_gate,
    aws_ecs_task_definition.morning_digest,
    aws_cloudwatch_event_rule.morning_digest_weekday,
  ]
}

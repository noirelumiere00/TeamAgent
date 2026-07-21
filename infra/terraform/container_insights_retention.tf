# ============================================================
# Existing ECS Container Insights operational log retention
# ============================================================
# Container Insights created these groups outside Terraform with one-day
# retention. Adopt the exact remote identities in place; never replace a group
# to change retention or KMS configuration.

resource "aws_cloudwatch_log_group" "ecs_containerinsights_teamagent" {
  name              = "/aws/ecs/containerinsights/teamagent-dev/performance"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.ecs_containerinsights_teamagent
  id = "/aws/ecs/containerinsights/teamagent-dev/performance"
}

resource "aws_cloudwatch_log_group" "ecs_containerinsights_tiktok" {
  name              = "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.ecs_containerinsights_tiktok
  id = "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance"
}

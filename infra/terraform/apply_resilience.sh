#!/usr/bin/env bash
# resilience 4本柱 + 新イメージ を targeted apply（bastion/worker/reminder_scan 等の巻き添えを避ける）。
# 使い方:  bash apply_resilience.sh          # plan 確認付き apply
#          bash apply_resilience.sh plan      # plan のみ（read-only）
set -euo pipefail
cd "$(dirname "$0")"

TARGETS=(
  # --- 新イメージ（柱1 entrypoint検証・柱2 emit・柱3 canaryスクリプト・柱4 commit label）---
  -target=aws_ecs_task_definition.mcp
  -target='aws_ecs_service.mcp[0]'
  -target=aws_ecs_task_definition.openclaw
  -target='aws_ecs_service.openclaw[0]'
  # --- 柱2 観測性（連携/設定違反 alarm）---
  -target=aws_cloudwatch_log_metric_filter.openclaw_config_violation
  -target=aws_cloudwatch_log_metric_filter.oauth_connect_failed
  -target=aws_cloudwatch_metric_alarm.openclaw_config_violation
  -target=aws_cloudwatch_metric_alarm.oauth_connect_failed
  # --- 柱2 通知先（SNS email 購読）---
  -target='aws_sns_topic_subscription.alarms_email["s-komata@vectorinc.co.jp"]'
  # --- 柱3 カナリア一式 ---
  -target=aws_cloudwatch_log_group.canary
  -target=aws_cloudwatch_log_metric_filter.canary_unhealthy
  -target=aws_cloudwatch_metric_alarm.canary_unhealthy
  -target='aws_iam_role.ecs_execution_canary[0]'
  -target='aws_iam_role_policy_attachment.ecs_execution_canary_managed[0]'
  -target='aws_iam_role_policy.ecs_execution_canary_secrets[0]'
  -target='aws_iam_role.canary_task[0]'
  -target='aws_security_group.canary[0]'
  -target='aws_ecs_task_definition.canary[0]'
  -target='aws_iam_role.events_canary_invoke[0]'
  -target='aws_iam_role_policy.events_canary_run_task[0]'
  -target='aws_cloudwatch_event_rule.canary_hourly[0]'
  -target='aws_cloudwatch_event_target.canary_run_task[0]'
)

if [ "${1:-}" = "plan" ]; then
  terraform plan "${TARGETS[@]}"
else
  terraform apply -auto-approve "${TARGETS[@]}"
fi

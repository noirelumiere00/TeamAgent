#!/usr/bin/env bash
# resilience 4本柱 + 新イメージ を targeted apply（bastion/worker/reminder_scan 等の巻き添えを避ける）。
# 使い方:  bash apply_resilience.sh          # plan 確認付き apply
#          bash apply_resilience.sh plan      # plan のみ（read-only）
set -euo pipefail
cd "$(dirname "$0")"

TARGETS=(
  # HMAC issuer/verifier task definitions are intentionally absent. Terraform cannot pause
  # between register-task-definition and update-service to execute the live CAS gate, so MCP,
  # connect-web, morning-digest, worker, and canary:14 must use the staged HMAC runbook.
  # --- OpenClaw image (not an HMAC issuer/verifier) ---
  -target=aws_ecs_task_definition.openclaw
  -target='aws_ecs_service.openclaw[0]'
  # --- 柱2 観測性（連携/設定違反 alarm）---
  -target=aws_cloudwatch_log_metric_filter.openclaw_config_violation
  -target=aws_cloudwatch_log_metric_filter.oauth_connect_failed
  -target=aws_cloudwatch_metric_alarm.openclaw_config_violation
  -target=aws_cloudwatch_metric_alarm.oauth_connect_failed
  # --- 柱2 通知先（SNS email 購読）---
  -target='aws_sns_topic_subscription.alarms_email["s-komata@vectorinc.co.jp"]'
)

if [ "${1:-}" = "plan" ]; then
  terraform plan "${TARGETS[@]}"
else
  terraform apply -auto-approve "${TARGETS[@]}"
fi

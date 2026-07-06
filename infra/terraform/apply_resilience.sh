#!/usr/bin/env bash
# resilience（監視/canary/alarm）のみを targeted plan/apply する。
#
# ⚠️ 2026-06-26 恒久対策（CLAUDE.md §4 B11）:
#   旧版は mcp/openclaw の ECS service/taskdef も -target に含み、tfvars 不備時に本番 ECS を destroy する
#   事故源だった。ECS service/taskdef は CLI 管理に移行したため**ここから除外**。
#   さらに -auto-approve を廃止（必ず plan を目視してから apply）。既定は plan のみ。
#
# 使い方:  bash apply_resilience.sh          # plan のみ（既定・read-only）
#          bash apply_resilience.sh apply    # plan 表示 → 対話承認で apply（-auto-approve なし）
set -euo pipefail
cd "$(dirname "$0")"

# ⚠️ ここに ECS service/taskdef(mcp/openclaw/connect_web)を -target で足さないこと（§4 B11）。
TARGETS=(
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

echo "== terraform plan（destroy 行が出たら apply するな・§4 B11）=="
terraform plan "${TARGETS[@]}"

if [ "${1:-}" = "apply" ]; then
  echo
  echo "== apply（-auto-approve なし。上の plan を確認し yes を入力）=="
  terraform apply "${TARGETS[@]}"
else
  echo
  echo "（plan のみ。適用するなら: bash apply_resilience.sh apply）"
fi

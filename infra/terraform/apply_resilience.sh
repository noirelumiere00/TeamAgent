#!/usr/bin/env bash
# Targeted observability-only maintenance helper.
#
# This helper deliberately excludes every ECS task definition, ECS service,
# EventBridge ECS target, worker IAM role and worker security group. Runtime
# rollouts must use the dedicated read-only runtime plan guard followed by the
# separately controlled deployment procedure.
#
# Usage:
#   bash apply_resilience.sh          # plan only (default)
#   bash apply_resilience.sh plan     # plan only
set -euo pipefail

cd "$(dirname "$0")"

if (( $# > 1 )) || [[ "${1:-plan}" != "plan" ]]; then
  printf 'usage: %s [plan]\n' "$0" >&2
  exit 2
fi

# Keep this allowlist non-runtime. In particular, do not add aws_ecs_*,
# aws_cloudwatch_event_target.*, or IAM/SG resources used by ECS workers.
targets=(
  -target=aws_cloudwatch_log_metric_filter.openclaw_config_violation
  -target=aws_cloudwatch_log_metric_filter.oauth_connect_failed
  -target=aws_cloudwatch_metric_alarm.openclaw_config_violation
  -target=aws_cloudwatch_metric_alarm.oauth_connect_failed
  -target=aws_cloudwatch_log_group.canary
  -target=aws_cloudwatch_log_metric_filter.canary_unhealthy
  -target=aws_cloudwatch_metric_alarm.canary_unhealthy
)

private_dir="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-resilience.XXXXXX")"
chmod 700 "$private_dir"
plan_path="$private_dir/resilience.tfplan"
trap 'rm -rf "$private_dir"' EXIT

terraform plan -input=false -out="$plan_path" "${targets[@]}"
terraform show -no-color "$plan_path"
printf '\nPlan only; no infrastructure was changed.\n'

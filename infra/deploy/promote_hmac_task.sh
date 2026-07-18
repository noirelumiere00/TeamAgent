#!/usr/bin/env bash
# Promote an already registered, live-gated HMAC task. This script never creates secrets, changes
# canary:14, or enables the morning-digest schedule.
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFLIGHT_PY="${PREFLIGHT_PY:-$REPO_ROOT/.venv/bin/python}"
HMAC_PREFLIGHT_MANIFEST="${HMAC_PREFLIGHT_MANIFEST:-}"
HMAC_ROLLOUT_CONTROL="${HMAC_ROLLOUT_CONTROL:-}"
HMAC_PROMOTION_TASK="${HMAC_PROMOTION_TASK:-}"
HMAC_REGISTERED_TASK_ARN="${HMAC_REGISTERED_TASK_ARN:-}"
HMAC_MORNING_TARGET_JSON="${HMAC_MORNING_TARGET_JSON:-}"

[[ -f "$HMAC_PREFLIGHT_MANIFEST" && -f "$HMAC_ROLLOUT_CONTROL" ]] || {
  echo "ERROR: reviewed HMAC manifest/control files are required" >&2
  exit 2
}
[[ "$HMAC_REGISTERED_TASK_ARN" =~ :task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$ ]] || {
  echo "ERROR: exact registered task definition ARN is required" >&2
  exit 2
}

case "$HMAC_PROMOTION_TASK" in
  mcp)
    CLUSTER="${HMAC_MCP_CLUSTER:-teamagent-dev}"
    SERVICE="${HMAC_MCP_SERVICE:-teamagent-dev-mcp}"
    "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
      --manifest "$HMAC_PREFLIGHT_MANIFEST" \
      --refresh-manifest-now \
      --control "$HMAC_ROLLOUT_CONTROL" \
      --action pre-update \
      --task mcp \
      --task-definition-arn "$HMAC_REGISTERED_TASK_ARN"
    aws ecs update-service --region "$REGION" --cluster "$CLUSTER" --service "$SERVICE" \
      --task-definition "$HMAC_REGISTERED_TASK_ARN" >/dev/null
    aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE"
    "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
      --manifest "$HMAC_PREFLIGHT_MANIFEST" \
      --refresh-manifest-now \
      --control "$HMAC_ROLLOUT_CONTROL" \
      --action mcp-stable-and-old-drained
    ;;
  morning_digest)
    RULE="${HMAC_MORNING_RULE:-teamagent-dev-morning-digest-weekday}"
    [[ -f "$HMAC_MORNING_TARGET_JSON" ]] || {
      echo "ERROR: exact reviewed EventBridge target JSON is required" >&2
      exit 2
    }
    command -v jq >/dev/null || { echo "ERROR: jq is required" >&2; exit 2; }
    [[ "$(jq -r 'length' "$HMAC_MORNING_TARGET_JSON")" == "1" ]] || {
      echo "ERROR: morning target JSON must contain exactly one target" >&2
      exit 2
    }
    [[ "$(jq -r '.[0].EcsParameters.TaskDefinitionArn' "$HMAC_MORNING_TARGET_JSON")" == "$HMAC_REGISTERED_TASK_ARN" ]] || {
      echo "ERROR: morning target task definition drift" >&2
      exit 2
    }
    "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
      --manifest "$HMAC_PREFLIGHT_MANIFEST" \
      --refresh-manifest-now \
      --control "$HMAC_ROLLOUT_CONTROL" \
      --action pre-update \
      --task morning_digest \
      --task-definition-arn "$HMAC_REGISTERED_TASK_ARN"
    aws events put-targets --region "$REGION" --rule "$RULE" \
      --targets "file://$HMAC_MORNING_TARGET_JSON" >/dev/null
    echo "morning_digest_target_updated=true schedule_enabled=false"
    ;;
  *)
    echo "ERROR: HMAC_PROMOTION_TASK must be mcp or morning_digest" >&2
    exit 2
    ;;
esac

echo "hmac_task_promotion=true"

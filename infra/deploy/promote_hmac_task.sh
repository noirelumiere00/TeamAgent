#!/usr/bin/env bash
# Retired: direct ECS/EventBridge mutation bypassed the one-use saved-plan release lock.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TERRAFORM_RUNBOOK="$SCRIPT_DIR/../terraform/README.md"

cat >&2 <<EOF
FATAL: promote_hmac_task.sh is permanently disabled.

HMAC task registration and runtime promotion must be one complete saved Terraform
plan created by plan_image_release.sh and applied once by
apply_image_release_plan.sh. The production_image_release_gate and
hmac_live_task_gate consume the same intent under the same shared lock.

Follow: $TERRAFORM_RUNBOOK
EOF
exit 64

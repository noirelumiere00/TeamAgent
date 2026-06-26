#!/usr/bin/env bash
# openclaw を dmPolicy:open イメージ(3fbef2ee)へ targeted apply するだけのスクリプト。
# 貼り付け崩れ防止用。実行: bash apply_openclaw.sh
set -euo pipefail
cd "$(dirname "$0")"

terraform apply -auto-approve \
  -target=aws_ecs_task_definition.openclaw \
  -target='aws_ecs_service.openclaw[0]'

#!/usr/bin/env bash
# mcp を image 差し替えで安全にデプロイする（CLI のみ・terraform 不使用）。
#
# ⚠️ terraform.tfvars 冒頭の注記どおり: ECS の image/env デプロイは terraform でなく
#   CLI(register-task-definition + update-service) で行う（CLAUDE.md §4 B11）。
#   mcp_image は tfvars 上の変数として存在するが、実運用の image ロールアウトは
#   これ（CLI）で行う。tfvars の mcp_image を書き換えて apply する運用はしない
#   （connect-web が同じ var.mcp_image を参照しており、過去に drift/事故の温床になった実績あり）。
#   fargate.tf の mcp service には ignore_changes=[task_definition,desired_count] が
#   設定済みなので、この CLI 更新は次回 terraform apply で巻き戻されない。
#
# 使い方: bash apply_mcp.sh <ECR_IMAGE_DIGEST_URI>
#   例:   bash apply_mcp.sh 718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:....
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-mcp
FAMILY=teamagent-dev-mcp

NEW_IMAGE="${1:-}"
if [ -z "$NEW_IMAGE" ]; then
  echo "ERROR: mcp image（ECR digest URI）を引数で渡してください。" >&2
  echo "  bash apply_mcp.sh <repo>@sha256:<digest>" >&2
  exit 2
fi

echo "== 1) 現 task def 取得（env/secrets を保持）=="
aws ecs describe-task-definition --region "$R" --task-definition "$FAMILY" \
  --query 'taskDefinition' --output json > /tmp/mcp_td.json
echo "   現在: $(jq -r '.taskDefinitionArn' /tmp/mcp_td.json) / image=$(jq -r '.containerDefinitions[0].image' /tmp/mcp_td.json)"

echo "== 2) image を差し替えて新 revision を register（他は不変）=="
jq --arg img "$NEW_IMAGE" '
  {family,taskRoleArn,executionRoleArn,networkMode,containerDefinitions,volumes,
   placementConstraints,requiresCompatibilities,cpu,memory,runtimePlatform,ephemeralStorage}
  | with_entries(select(.value != null))
  | .containerDefinitions[0].image = $img
' /tmp/mcp_td.json > /tmp/mcp_td_new.json
NEW_TD=$(aws ecs register-task-definition --region "$R" \
  --cli-input-json file:///tmp/mcp_td_new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new task def=$NEW_TD"

echo "== 3) rolling update -> wait stable =="
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" \
  --task-definition "$NEW_TD" >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
echo "== DONE: mcp 更新完了。CloudWatch でエラーゼロ確認し、deploy_log.md に1行追記すること（§4 B3）。"

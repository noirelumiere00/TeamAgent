#!/usr/bin/env bash
# openclaw を image 差し替えで安全にデプロイする（CLI のみ・terraform 不使用）。
#
# ⚠️ 2026-06-26 恒久対策（CLAUDE.md §4 B11）:
#   旧版は `terraform apply -auto-approve -target=openclaw` だった。-var openclaw_image= 無しで実行すると
#   var.openclaw_image="" → count=0 で **openclaw service を destroy**＝AiLa 全断する事故を起こした。
#   openclaw は CLI 管理へ移行（terraform は openclaw を見ない＝tfvars に openclaw_image を書かない）。
#
# 使い方: bash apply_openclaw.sh <ECR_IMAGE_DIGEST_URI>
#   例:   bash apply_openclaw.sh 718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@sha256:....
#   （現行を据え置く場合は実行不要。このスクリプトは image 更新時のみ）
#
# 障害復旧（service 自体が消えている場合）は update-service ではなく create-service が必要:
#   aws ecs create-service --region ap-northeast-1 --cluster teamagent-dev \
#     --service-name teamagent-dev-openclaw --task-definition teamagent-dev-openclaw:<rev> \
#     --desired-count 1 --launch-type FARGATE \
#     --network-configuration 'awsvpcConfiguration={subnets=[subnet-07e0d4e58b3b83b8a,subnet-0c5982c60d38557ce,subnet-0d87f3016e96101a5],securityGroups=[sg-047233cc8756e9c47],assignPublicIp=ENABLED}' \
#     --deployment-configuration 'maximumPercent=200,minimumHealthyPercent=100' --scheduling-strategy REPLICA
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-openclaw
FAMILY=teamagent-dev-openclaw

NEW_IMAGE="${1:-}"
if [ -z "$NEW_IMAGE" ]; then
  echo "ERROR: openclaw image（ECR digest URI）を引数で渡してください。" >&2
  echo "  bash apply_openclaw.sh <repo>@sha256:<digest>" >&2
  exit 2
fi

echo "== 1) 現 task def 取得（env/secrets を保持）=="
aws ecs describe-task-definition --region "$R" --task-definition "$FAMILY" \
  --query 'taskDefinition' --output json > /tmp/oc_td.json
echo "   現在: $(jq -r '.taskDefinitionArn' /tmp/oc_td.json) / image=$(jq -r '.containerDefinitions[0].image' /tmp/oc_td.json)"

echo "== 2) image を差し替えて新 revision を register（他は不変）=="
jq --arg img "$NEW_IMAGE" '
  {family,taskRoleArn,executionRoleArn,networkMode,containerDefinitions,volumes,
   placementConstraints,requiresCompatibilities,cpu,memory,runtimePlatform,ephemeralStorage}
  | with_entries(select(.value != null))
  | .containerDefinitions[0].image = $img
' /tmp/oc_td.json > /tmp/oc_td_new.json
NEW_TD=$(aws ecs register-task-definition --region "$R" \
  --cli-input-json file:///tmp/oc_td_new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new task def=$NEW_TD"

echo "== 3) rolling update -> wait stable =="
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" \
  --task-definition "$NEW_TD" >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
echo "== DONE: openclaw 更新完了。Slack で @AiLa の応答を確認し、deploy_log.md に1行追記すること（§4 B3）。"

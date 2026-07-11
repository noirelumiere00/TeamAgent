#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# ingest タスクへコード変更を届ける唯一の経路（deploy_connectweb_unified.sh は
# connect-web 専用で ingest td を更新しない、という既知の穴を塞ぐ）。
#   ECR teamagent-mcp の --image-tag を digest 解決し（immutable tag 前提・digest 運用）、
#   現行 teamagent-dev-ingest td の image だけを差し替えて新 revision を登録する。
#   env / secrets / cpu / memory / role は既存維持（宣言的差分は image のみ）。
#   scripts/aws/run_ingest_task.sh は family 名指定＝最新 revision を拾うので、
#   登録後の次回実行から新コードが使われる。
#   ⚠️ EventBridge 週次ルール（現在 DISABLED）のターゲットは特定 revision 固定。
#      週次を再開する場合はターゲットの task_definition_arn 更新も必要（terraform 側）。
# 使い方: bash infra/deploy/register_ingest_td.sh --image-tag <ECRタグ>
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
TD_FAMILY=teamagent-dev-ingest
ECR_REPO=teamagent-mcp
ECR="718959508629.dkr.ecr.$R.amazonaws.com/$ECR_REPO"
TAG=""

usage() {
  cat <<'EOF'
usage: register_ingest_td.sh --image-tag <ECRタグ>
  --image-tag  ECR teamagent-mcp のイメージタグ（必須。digest に解決して登録する）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image-tag) TAG="${2:?--image-tag に値が必要}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "★不明な引数: $1"; usage; exit 1 ;;
  esac
done
[ -n "$TAG" ] || { echo "★--image-tag は必須"; usage; exit 1; }

command -v jq >/dev/null || { echo "★jq が必要"; exit 1; }
command -v aws >/dev/null || { echo "★aws CLI が必要"; exit 1; }

echo "== 1) digest 解決（tag の中身は保証されないため digest で固定）=="
DIGEST=$(aws ecr describe-images --region "$R" --repository-name "$ECR_REPO" \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)
[ -n "$DIGEST" ] && [ "$DIGEST" != "None" ] || { echo "★ECR($ECR_REPO) に tag=$TAG が無い"; exit 1; }
IMG="$ECR@$DIGEST"
echo "  $IMG"

echo "== 2) 現行 td 取得（ロールバック revision 控え）=="
aws ecs describe-task-definition --region "$R" --task-definition "$TD_FAMILY" \
  --query 'taskDefinition' > /tmp/ingest_td_cur.json
CUR_ARN=$(jq -r '.taskDefinitionArn' /tmp/ingest_td_cur.json)
echo "  現行: $CUR_ARN"

echo "== 3) image のみ差替 → register-task-definition（env/secrets は既存維持）=="
jq --arg img "$IMG" '
  .containerDefinitions[0].image = $img
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
        .registeredAt, .registeredBy, .deregisteredAt)
' /tmp/ingest_td_cur.json > /tmp/ingest_td_new.json
NEW_ARN=$(aws ecs register-task-definition --region "$R" \
  --cli-input-json file:///tmp/ingest_td_new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo ""
echo "✅ 新 revision: $NEW_ARN"
echo "   scripts/aws/run_ingest_task.sh は family 名指定なので次回実行から新 image が使われる"
echo "⏪ 戻す場合: 旧 revision($CUR_ARN) を run-task の --task-definition で明示指定するか、"
echo "   新 revision を aws ecs deregister-task-definition --task-definition $NEW_ARN で無効化"

#!/usr/bin/env bash
# mcp 便の media 差し替え汎用版（2026-09-24・改善 2）: tiktok-acquire の現行 TD を元に image だけ変えた revision を登録し、
# dispatch lambda の TASKDEF_ARN を更新する（deploy_media_tdNN.sh を便ごとに複製していたのを 1 本にした）。
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/../common.sh" || exit 1
DIGEST="${1:?usage: deploy_media_td.sh <sha256:...release media digest>}"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "FATAL: digest 形式が不正"; exit 1; }
export AWS_PAGER=""
P=(--region ap-northeast-1)
W="$RELEASE_WORK_DIR/media-runs/$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$W"  # 09-24 レビュー D-07: 実行ごとに別ディレクトリ（前便の差し替え前記録を上書きしない）
NEW_IMAGE="718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-media-worker@${DIGEST}"

aws ecs describe-task-definition "${P[@]}" --task-definition teamagent-dev-tiktok-acquire --query 'taskDefinition' --output json > "$W/td-cur.json"
python3 - "$W/td-cur.json" "$NEW_IMAGE" > "$W/td-new-input.json" <<'PY'
import json, sys
td = json.load(open(sys.argv[1]))
old = td["containerDefinitions"][0]["image"]
td["containerDefinitions"][0]["image"] = sys.argv[2]
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k, None)
print(json.dumps(td))
print(f"image: {old}\n  ->  {sys.argv[2]}", file=sys.stderr)
PY
NEW_ARN=$(aws --profile "$RELEASE_PROFILE" ecs register-task-definition "${P[@]}" --cli-input-json "file://$W/td-new-input.json" --query 'taskDefinition.taskDefinitionArn' --output text)
echo "registered: $NEW_ARN"
aws lambda get-function-configuration "${P[@]}" --function-name teamagent-dev-tiktok-acquire-dispatch --query 'Environment.Variables' --output json > "$W/lambda-env-before.json"
python3 - "$W/lambda-env-before.json" "$NEW_ARN" > "$W/lambda-env-after.json" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])); assert "TASKDEF_ARN" in env
env["TASKDEF_ARN"] = sys.argv[2]; json.dump({"Variables": env}, sys.stdout)
PY
aws --profile "$RELEASE_PROFILE" lambda update-function-configuration "${P[@]}" --function-name teamagent-dev-tiktok-acquire-dispatch --environment "file://$W/lambda-env-after.json" --query 'LastUpdateStatus' --output text
aws lambda wait function-updated "${P[@]}" --function-name teamagent-dev-tiktok-acquire-dispatch
echo "=== 検証 ==="
aws ecs describe-task-definition "${P[@]}" --task-definition "$NEW_ARN" --query 'taskDefinition.{rev:revision,image:containerDefinitions[0].image}' --output json
aws lambda get-function-configuration "${P[@]}" --function-name teamagent-dev-tiktok-acquire-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text
echo "🏁 tiktok-acquire ${NEW_ARN##*/} 差し替え + lambda 更新 完了"

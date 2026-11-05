#!/usr/bin/env bash
# 55 事例の種別（ルール案のうち確定 49 件）を本番の vl_campaign_settings に入れる（2026-11-02・小俣さん OK）。
# ingest のタスク定義で 1 回だけ python を動かす（DB の資格情報は AWS の中だけ）。既に設定がある事例は上書きしない。
#   ./vl_bulk_types.sh            # 空撃ち（件数だけ・書かない）
#   ./vl_bulk_types.sh --apply    # 書く
# 戻し方: DELETE FROM vl_campaign_settings WHERE updated_by='<actor-email>' AND updated_at::date='2026-11-02'（管理画面で個別に直してもよい）
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aiiadev-mfa} AWS_REGION=ap-northeast-1 AWS_PAGER=""
D="$(cd "$(dirname "$0")" && pwd)"
APPLY=$([ "${1:-}" = "--apply" ] && echo 1 || echo 0)
CLUSTER=teamagent-dev
RUNNING="$(aws ecs list-tasks --cluster "$CLUSTER" --family teamagent-dev-ingest --desired-status RUNNING --query 'length(taskArns)' --output text)"
[ "$RUNNING" = "0" ] || { echo "FATAL: ingest タスクが実行中（${RUNNING}）。終わってから"; exit 1; }
ENV_JSON="$(aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables' --output json)"
TD="$(echo "$ENV_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["TASKDEF_ARN"])')"
NET="$(echo "$ENV_JSON" | python3 -c 'import json,sys;e=json.load(sys.stdin);print(json.dumps({"awsvpcConfiguration":{"subnets":e["SUBNETS"].split(","),"securityGroups":[e["SG_ID"]],"assignPublicIp":"ENABLED"}}))')"
CONTAINER="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].name' --output text)"
OVR="$(python3 - "$CONTAINER" "$D/vl_bulk_types_20261102.py" "$D/vl_bulk_types_20261102.json" "$APPLY" <<'PY'
import json, sys
name, code, rows, apply = sys.argv[1], open(sys.argv[2]).read(), open(sys.argv[3]).read(), sys.argv[4]
print(json.dumps({"containerOverrides": [{"name": name, "command": ["python", "-c", code],
    "environment": [{"name": "VL_BULK_ROWS", "value": rows}, {"name": "VL_BULK_APPLY", "value": apply}]}]}, ensure_ascii=False))
PY
)"
echo "== run-task ${TD##*/} apply=$APPLY"
TASK="$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TD" --network-configuration "$NET" --overrides "$OVR" --query 'tasks[0].taskArn' --output text)"
[ "$TASK" != "None" ] && [ -n "$TASK" ] || { echo "run-task 失敗"; exit 1; }
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK" || aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)"
echo "== exitCode=$CODE"
LG="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-group"' --output text)"
PFX="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-stream-prefix"' --output text)"
aws logs get-log-events --log-group-name "$LG" --log-stream-name "$PFX/$CONTAINER/${TASK##*/}" --limit 40 --query 'events[].message' --output text | tr '\t' '\n' | grep -E "bulk_types|Error|Traceback|File " | tail -8 | cut -c1-900
[ "$CODE" = "0" ]

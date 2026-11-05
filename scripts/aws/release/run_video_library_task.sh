#!/usr/bin/env bash
# 動画ライブラリの同期・サムネを ingest のタスク定義で単独実行する（2026-10-30）。ingest 本体は回さない。
#   ./run_video_library_task.sh sync --dry-run     # 件数と warning だけ（DB に書かない）
#   ./run_video_library_task.sh sync               # 同期（DB に書く）
#   ./run_video_library_task.sh thumbs 300         # サムネを最大 300 本取得
# ネットワーク設定は ingest-dispatch Lambda の env（SUBNETS・SG_ID）から取る。完了まで待ち、exitCode とログ末尾を表示。
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aiiadev-mfa} AWS_REGION=ap-northeast-1 AWS_PAGER=""
KIND="${1:?sync|thumbs}"; ARG="${2:-}"
CLUSTER=teamagent-dev
case "$KIND" in
  # 既定の yaml パスは site-packages 基準でずれる（no-editable install）。焼き込みの /app/data を明示する
  sync)   PYARGS=$( [ "$ARG" = "--dry-run" ] && echo "['--dry-run']" || echo "[]" )
          CMD='["python","-c","from pathlib import Path; from teamagent.ingest.video_library_sync import main; raise SystemExit(main('"$PYARGS"', sources_path=Path(\"/app/data/ingest_sources.yaml\")))"]' ;;
  thumbs) CMD='["python","-m","teamagent.ingest.video_library_thumbs","--limit","'"${ARG:-300}"'"]' ;;
  *) echo "usage: $0 sync [--dry-run] | thumbs [N]"; exit 2 ;;
esac
RUNNING="$(aws ecs list-tasks --cluster "$CLUSTER" --family teamagent-dev-ingest --desired-status RUNNING --query 'length(taskArns)' --output text)"
[ "$RUNNING" = "0" ] || { echo "FATAL: ingest タスクが実行中（${RUNNING}）。終わってから"; exit 1; }
ENV_JSON="$(aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables' --output json)"
TD="$(echo "$ENV_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["TASKDEF_ARN"])')"
NET="$(echo "$ENV_JSON" | python3 -c 'import json,sys;e=json.load(sys.stdin);print(json.dumps({"awsvpcConfiguration":{"subnets":e["SUBNETS"].split(","),"securityGroups":[e["SG_ID"]],"assignPublicIp":"ENABLED"}}))')"
CONTAINER="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].name' --output text)"
OVR="{\"containerOverrides\":[{\"name\":\"$CONTAINER\",\"command\":$CMD}]}"
echo "== run-task ${TD##*/} $CMD"
TASK="$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TD" --network-configuration "$NET" --overrides "$OVR" --query 'tasks[0].taskArn' --output text)"
[ "$TASK" != "None" ] && [ -n "$TASK" ] || { echo "run-task 失敗"; exit 1; }
echo "   task ${TASK##*/}"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK" || aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)"
echo "== exitCode=$CODE"
LG="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-group"' --output text)"
PFX="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-stream-prefix"' --output text)"
aws logs get-log-events --log-group-name "$LG" --log-stream-name "$PFX/$CONTAINER/${TASK##*/}" --limit 30 --query 'events[].message' --output text | tr '\t' '\n' | grep -E "video_library|thumb|error|Error" | tail -8 | cut -c1-600
[ "$CODE" = "0" ]

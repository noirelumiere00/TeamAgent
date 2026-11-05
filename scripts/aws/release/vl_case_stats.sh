#!/usr/bin/env bash
# 直近の投稿をアカウントから取り込む一回きりの補完（2026-11-02）。mcp のタスク定義で 1 回だけ python を動かす。
#   ./vl_backfill_latest.sh            # 空撃ち（対象アカウント数・概算費用だけ）
#   ./vl_backfill_latest.sh --apply    # Apify で取得して、ライブラリに無い動画だけ書く
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aiiadev-mfa} AWS_REGION=ap-northeast-1 AWS_PAGER=""
D="$(cd "$(dirname "$0")" && pwd)"
APPLY=$([ "${1:-}" = "--apply" ] && echo 1 || echo 0)
CLUSTER=teamagent-dev
GRACE="emme_tokyo.jp,noako_cosme,maripii_cosme,nene_cosme_nene,emme_beauty_life,emme_cosme,tesuko_test,emme_make_cosme,emme_make_,emme_makeup__,emme_mirupi,mayo___media,uni___media,meluffe_media,nomnom_dog"
TD="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-mcp --query 'services[0].taskDefinition' --output text)"
NET="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-mcp --query 'services[0].networkConfiguration' --output json)"
CONTAINER="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].name' --output text)"
OVR="$(python3 - "$CONTAINER" "$D/vl_case_stats_20261104.py" "$APPLY" "$GRACE" <<'PY'
import json, sys
name, code, apply, grace = sys.argv[1], open(sys.argv[2]).read(), sys.argv[3], sys.argv[4]
print(json.dumps({"containerOverrides": [{"name": name, "command": ["python", "-c", code],
    "environment": [{"name": "VL_APPLY", "value": apply}, {"name": "VL_EXTRA_HANDLES", "value": grace}]}]}, ensure_ascii=False))
PY
)"
echo "== run-task ${TD##*/} apply=$APPLY (override $(echo -n "$OVR" | wc -c) bytes)"
TASK="$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TD" --network-configuration "$NET" --overrides "$OVR" --started-by vl-backfill --query 'tasks[0].taskArn' --output text)"
[ "$TASK" != "None" ] && [ -n "$TASK" ] || { echo "run-task 失敗"; exit 1; }
echo "   task ${TASK##*/}"
for _ in $(seq 1 30); do aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK" 2>/dev/null && break; done
CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)"
echo "== exitCode=$CODE"
LG="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-group"' --output text)"
PFX="$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-stream-prefix"' --output text)"
aws logs get-log-events --log-group-name "$LG" --log-stream-name "$PFX/$CONTAINER/${TASK##*/}" --limit 60 --query 'events[].message' --output text | tr '\t' '\n' | grep -E "casestats|Error|Traceback|File " | tail -14 | cut -c1-600
[ "$CODE" = "0" ]

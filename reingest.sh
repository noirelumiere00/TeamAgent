#!/usr/bin/env bash
# 新コードの mcp image で ingest を再実行し、Contextual Retrieval + 分類(cls_*)を実データに反映。
# 使い方:  bash reingest.sh small   # Slack のみ＝コスト計測用（~205 chunk）
#          bash reingest.sh full    # 全ソース slack,gdrive,gsheets（794docs）
# 既存の週次スケジュール(:10)・本番mcpサービスには触れない（別 revision を明示 run-task）。
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-northeast-1
MODE=${1:?usage: reingest.sh small|full}
NEW_IMAGE=718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:9d4070ad9d39abfb75af4c9ee1a8ce1fc7bb94917b61bf799e8061303d101324
CLUSTER=teamagent-dev
FAMILY=teamagent-dev-ingest
BASE_REV=13   # 唯一の ACTIVE revision（10 等は deregister 済み）
SUBNETS=subnet-0c5982c60d38557ce,subnet-0d87f3016e96101a5,subnet-07e0d4e58b3b83b8a
SG=sg-0f338af9e9b8d4269
# small = gdrive（命名2フォルダの実PDF・folder経路＝classify+contextualize両方で $/chunk 計測。
#          Slack は bot 未参加で not_in_channel になるため使わない）。
# full  = all（空/all で kinds=None＝slack,gdrive,gsheets,shared_drives 全実行。794docs 本体の
#          shared_drives crawl を必ず含める。"slack,gdrive,gsheets" だと crawl が走らず本コーパス
#          に contextual/classify が反映されない＝再取込が無意味になる）。
if [ "$MODE" = "small" ]; then SOURCES=gdrive; else SOURCES=all; fi

echo "== 新コードの ingest task def revision を登録（image=${NEW_IMAGE}）=="
aws ecs describe-task-definition --task-definition "$FAMILY:$BASE_REV" \
  --query 'taskDefinition' --output json > /tmp/ing_base.json
python3 - "$NEW_IMAGE" "$MODE" <<'PY'
import json, sys
td = json.load(open("/tmp/ing_base.json"))
for k in ("taskDefinitionArn","revision","status","requiresAttributes",
          "compatibilities","registeredAt","registeredBy","deregisteredAt"):
    td.pop(k, None)
td["containerDefinitions"][0]["image"] = sys.argv[1]
# full は 794docs + 共有ドライブ crawl で重い → 16 vCPU / 32GB に拡張して wall-clock 短縮。
# （Haiku 総コストは vCPU 非依存でほぼ同じ。並列化で時間だけ縮む）。container 側に hard
# memory があれば task memory 以下にクランプして上限超過エラーを防ぐ。
if sys.argv[2] == "full":
    td["cpu"] = "16384"
    td["memory"] = "32768"
    c = td["containerDefinitions"][0]
    if c.get("memory") and int(c["memory"]) > 32768:
        c["memory"] = 32768
json.dump(td, open("/tmp/ing_new.json", "w"))
PY
NEWREV=$(aws ecs register-task-definition --cli-input-json file:///tmp/ing_new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new task def = $NEWREV"

echo "== run-task（${MODE}: INGEST_SOURCES=${SOURCES}・USE_CONTEXTUAL_INGEST=1・USE_DOC_CLASSIFY=1）=="
TASK=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$NEWREV" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"ingest\",\"environment\":[{\"name\":\"INGEST_SOURCES\",\"value\":\"$SOURCES\"},{\"name\":\"USE_CONTEXTUAL_INGEST\",\"value\":\"1\"},{\"name\":\"USE_DOC_CLASSIFY\",\"value\":\"1\"}]}]}" \
  --query 'tasks[0].taskArn' --output text)
echo "   ✅ task = $TASK"
echo
echo "ログ追従（コスト・件数を見る）:"
echo "  AWS_REGION=ap-northeast-1 aws logs tail /teamagent/dev/ingest --follow --since 3m"

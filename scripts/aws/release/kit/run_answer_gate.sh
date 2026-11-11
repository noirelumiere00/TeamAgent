#!/bin/bash
# 便の本番反映の前に、候補の mcp イメージで答え試験（架空データ 16 問・本物の LLM）を AWS 内で 1 回だけ回す。
# 使い方: run_answer_gate.sh sha256:<core digest>   → 全問合格なら exit 0、それ以外は exit 1（本番反映しない）。
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
DIGEST="${1:-}"; [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "usage: $0 sha256:<digest>"; exit 2; }
C=teamagent-dev; W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
BASE=$(aws ecs describe-services --cluster $C --services teamagent-dev-mcp --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$BASE" --query taskDefinition --output json > "$W/base.json"
aws ecs describe-services --cluster $C --services teamagent-dev-mcp --query 'services[0].networkConfiguration' --output json > "$W/net.json"
python3 - "$W/base.json" "$DIGEST" > "$W/td.json" <<'PY'
import json, sys
td = json.load(open(sys.argv[1])); d = sys.argv[2]
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k, None)
td["family"] = "teamagent-dev-answer-gate"
c = td["containerDefinitions"][0]
c["image"] = c["image"].split("@")[0] + "@" + d
c.pop("portMappings", None); c.pop("healthCheck", None)
# 試験だけを動かす。送信・書込は試験側が全部ローカルに記録して外へ出さない（answer_gate の設計）。
code = (
    "import json,subprocess,sys\n"
    "rc=subprocess.call([sys.executable,'-m','teamagent.eval.answer_gate','--out','/tmp/ag.jsonl'])\n"
    "for l in open('/tmp/ag.jsonl'):\n"
    " r=json.loads(l); res=r.get('result') or {}\n"
    " bad=[k for k,v in (res.get('checks') or {}).items() if not v.get('pass')]\n"
    " print('ANSWER_GATE', r.get('id'), 'PASS' if not bad and r.get('completed') else 'FAIL', ','.join(bad), flush=True)\n"
    "print('ANSWER_GATE_EXIT', rc, flush=True); sys.exit(rc)\n"
)
c["command"] = ["/app/.venv/bin/python", "-c", code]
c["logConfiguration"]["options"]["awslogs-stream-prefix"] = "answer-gate"
json.dump(td, sys.stdout)
PY
TD=$(aws --profile "$RELEASE_PROFILE" ecs register-task-definition --cli-input-json "file://$W/td.json" --query 'taskDefinition.taskDefinitionArn' --output text) || { echo "FATAL: 一時 TD を登録できない"; exit 1; }
cleanup() { aws --profile "$RELEASE_PROFILE" ecs deregister-task-definition --task-definition "$TD" >/dev/null 2>&1 || true; rm -rf "$W"; }; trap cleanup EXIT
TASK=$(aws --profile "$RELEASE_PROFILE" ecs run-task --cluster $C --task-definition "$TD" --launch-type FARGATE --network-configuration "file://$W/net.json" --query 'tasks[0].taskArn' --output text) || { echo "FATAL: run-task 失敗"; exit 1; }
echo "answer gate task: ${TASK##*/}（${DIGEST}）"
aws ecs wait tasks-stopped --cluster $C --tasks "$TASK" || aws ecs wait tasks-stopped --cluster $C --tasks "$TASK"
EXIT=$(aws ecs describe-tasks --cluster $C --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
STREAM="answer-gate/teamagent-mcp/${TASK##*/}"
aws logs filter-log-events --log-group-name /teamagent/dev/teamagent-mcp --log-stream-names "$STREAM" --filter-pattern ANSWER_GATE --query 'events[].message' --output text | tr '\t' '\n' | sed 's/^/  /'
if [ "$EXIT" = "0" ]; then echo "🏁 答え試験 合格（${TASK##*/}）"; exit 0; fi
echo "FATAL: 答え試験 不合格（exit=${EXIT}・${TASK##*/}）。本番反映しない"; exit 1

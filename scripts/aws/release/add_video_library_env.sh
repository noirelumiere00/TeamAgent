#!/usr/bin/env bash
# 動画ライブラリ（#742・#753）の env を connect-web と ingest の現行タスク定義に足す（2026-10-30）。image は変えない。
#   connect-web: VIDEO_LIBRARY_S3_BUCKET               → 新 revision を update-service（services-stable まで待つ）
#   ingest     : VIDEO_LIBRARY_S3_BUCKET・VIDEO_LIBRARY_ACCOUNTS_SHEET_ID → 新 revision を dispatch Lambda の TASKDEF_ARN に
# swap_mcp_consumers_td.sh は「最新 revision から image だけ差し替える」ので、先にここで env を足せば次の便でそのまま引き継がれる。
#   ./add_video_library_env.sh --dry-run   # 差分の表示だけ
#   ./add_video_library_env.sh             # 登録・差し替え
# 戻し方: connect-web は前 revision に update-service、ingest は Lambda の TASKDEF_ARN を前 revision に戻す。
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aiiadev-mfa} AWS_REGION=ap-northeast-1 AWS_PAGER=""
DRY="${1:-}"
CLUSTER=teamagent-dev
OUT="$(mktemp -d)"; chmod 700 "$OUT"
BUCKET=teamagent-dev-raw-files
LEDGER=1Zv-RsxkjwET8OrAMpxVPmfTHLpDPxyni7_HutEGeEwk

register_with_env() {  # <family> KEY=VAL...
  local fam="$1"; shift
  aws ecs describe-task-definition --task-definition "$fam" --query 'taskDefinition' --output json > "$OUT/$fam-cur.json"
  python3 - "$OUT/$fam-cur.json" "$@" > "$OUT/$fam-input.json" <<'PY'
import json, sys
td = json.load(open(sys.argv[1]))
rev = td["revision"]
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k, None)
c = td["containerDefinitions"][0]
env = {e["name"]: e["value"] for e in c.get("environment", [])}
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    print(f"  {td['family']}:{rev} {k}: {env.get(k, '(なし)')} -> {v}", file=sys.stderr)
    env[k] = v
c["environment"] = [{"name": k, "value": v} for k, v in env.items()]
json.dump(td, sys.stdout, ensure_ascii=False)
PY
  if [ "$DRY" = "--dry-run" ]; then echo "arn:dry-run/$fam:0"; return; fi
  aws ecs register-task-definition --cli-input-json "file://$OUT/$fam-input.json" --query 'taskDefinition.taskDefinitionArn' --output text
}

RUNNING="$(aws ecs list-tasks --cluster "$CLUSTER" --family teamagent-dev-ingest --desired-status RUNNING --query 'length(taskArns)' --output text)"
[ "$RUNNING" = "0" ] || { echo "FATAL: ingest タスクが実行中（${RUNNING}）。終わってから再実行（何も変えていない）"; exit 1; }

echo "=== 1. connect-web"
NEW_CW="$(register_with_env teamagent-dev-connect-web "VIDEO_LIBRARY_S3_BUCKET=$BUCKET")"
echo "  registered: ${NEW_CW##*/}"
if [ "$DRY" != "--dry-run" ]; then
  aws ecs update-service --cluster "$CLUSTER" --service teamagent-dev-connect-web --task-definition "$NEW_CW" --query 'service.taskDefinition' --output text | sed -E 's|.*/|  service -> |'
  aws ecs wait services-stable --cluster "$CLUSTER" --services teamagent-dev-connect-web
  aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-connect-web --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition,rolloutState]' --output text | sed -E 's|arn:[^\t]*/|  PRIMARY |'
fi

echo "=== 2. ingest"
NEW_IN="$(register_with_env teamagent-dev-ingest "VIDEO_LIBRARY_S3_BUCKET=$BUCKET" "VIDEO_LIBRARY_ACCOUNTS_SHEET_ID=$LEDGER")"
echo "  registered: ${NEW_IN##*/}"
aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables' --output json > "$OUT/lambda-env.json"
python3 - "$OUT/lambda-env.json" "$NEW_IN" > "$OUT/lambda-env-new.json" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])); assert "TASKDEF_ARN" in env
print(f"  TASKDEF_ARN: {env['TASKDEF_ARN'].rsplit('/',1)[-1]} -> {sys.argv[2].rsplit('/',1)[-1]}", file=sys.stderr)
env["TASKDEF_ARN"] = sys.argv[2]; json.dump({"Variables": env}, sys.stdout)
PY
if [ "$DRY" != "--dry-run" ]; then
  aws lambda update-function-configuration --function-name teamagent-dev-ingest-dispatch --environment "file://$OUT/lambda-env-new.json" --query 'LastUpdateStatus' --output text | sed 's/^/  /'
  aws lambda wait function-updated --function-name teamagent-dev-ingest-dispatch
  aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text | sed -E 's|.*/|  now: |'
fi
echo "=== 完了"

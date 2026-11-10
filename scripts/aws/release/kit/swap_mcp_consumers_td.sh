#!/usr/bin/env bash
# mcp イメージを使う 4 つの consumer（connect-web / morning-digest / canary / ingest）のタスク定義を、
# 指定の署名 digest へ差し替える（image のみ・env / secrets / cpu / memory は現行 TD を継承）。
#
# 2026-09-15 発見: mcp 便の TD 差し替え（register_mcp_td_image_env.sh）は mcp サービスしか更新せず、
# connect-web:81 は 09-04 の image、morning-digest:60 / canary:29 / ingest:61 は 08-26 の image
# （signed-0d56cff8）で動いていた。朝ダイジェストの描画・取込の改善は、ここを差し替えるまで本番に効かない。
#
# 使い方: bash swap_mcp_consumers_td.sh <sha256:...（teamagent-mcp の release digest）> [--dry-run]
#   例: bash swap_mcp_consumers_td.sh sha256:e4c512eb14273f1f0edfe5574b38c5ad2714d1b30042a94250767122ac8b10b1
#
# 各 consumer の参照先:
#   connect-web   … ECS service teamagent-dev-connect-web（update-service → services-stable）
#   morning-digest… EventBridge rule teamagent-dev-morning-digest-weekday の target（put-targets）
#   canary        … EventBridge rule teamagent-dev-canary-hourly の target（put-targets・rule は DISABLED のまま）
#   ingest        … Lambda teamagent-dev-ingest-dispatch の env TASKDEF_ARN（update-function-configuration）
# 各段 fail-closed。差し替え前に digest が本番リポジトリ（teamagent-mcp）に存在することを確認する。
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
DIGEST="${1:?usage: swap_mcp_consumers_td.sh <sha256:...> [--dry-run]}"
DRY="${2:-}"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "FATAL: digest 形式が不正"; exit 1; }
CLUSTER=teamagent-dev
OUT="$RELEASE_WORK_DIR/mcp-consumers-td/$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$OUT"; chmod 700 "$OUT"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="$ACCT.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@$DIGEST"

echo "=== 0. digest が本番リポジトリにあるか"
aws ecr describe-images --repository-name teamagent-mcp --image-ids imageDigest="$DIGEST" --query 'imageDetails[0].imageTags' --output text | grep -qE 'active-|signed-' || { echo "FATAL: teamagent-mcp に $DIGEST が無い、または active/signed タグが無い"; exit 1; }

# 現行 TD を読み、image だけ差し替えた新 revision を登録する。戻り値は新 TD ARN。
register_from() {  # <family>
  local fam="$1" cur new
  cur="$(aws ecs describe-task-definition --task-definition "$fam" --query 'taskDefinition' --output json)"
  python3 - "$cur" "$IMAGE" > "$OUT/$fam-input.json" <<'PY'
import json, sys
td = json.loads(sys.argv[1]); image = sys.argv[2]
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k, None)
c = td["containerDefinitions"][0]
old = c["image"]
assert old.split("@")[0].endswith("/teamagent-mcp"), old
c["image"] = image
print(f"  {td['family']}: {old.split('@')[1][:19]}… -> {image.split('@')[1][:19]}…", file=sys.stderr)
json.dump(td, sys.stdout, ensure_ascii=False)
PY
  if [ "$DRY" = "--dry-run" ]; then echo "  (dry-run) register-task-definition をスキップ" >&2; echo "arn:dry-run/$fam:0"; return; fi
  new="$(aws --profile "$RELEASE_PROFILE" ecs register-task-definition --cli-input-json "file://$OUT/$fam-input.json" --query 'taskDefinition.taskDefinitionArn' --output text)"
  echo "  registered: ${new##*/}" >&2
  echo "$new"
}

# 09-24 レビュー D-10: ingest の実行中確認は最初に行う（3 つを差し替えた後で止まると半端な状態になる）
RUNNING="$(aws ecs list-tasks --cluster "$CLUSTER" --family teamagent-dev-ingest --desired-status RUNNING --query 'length(taskArns)' --output text)"
[ "$RUNNING" = "0" ] || { echo "FATAL: ingest タスクが実行中（${RUNNING}）。終わってから再実行（まだ何も差し替えていない）"; exit 1; }

echo "=== 1. connect-web（ECS service）"
NEW_CW="$(register_from teamagent-dev-connect-web)"
if [ "$DRY" != "--dry-run" ]; then
  aws --profile "$RELEASE_PROFILE" ecs update-service --cluster "$CLUSTER" --service teamagent-dev-connect-web --task-definition "$NEW_CW" --query 'service.taskDefinition' --output text | sed -E 's|.*/||'
  aws ecs wait services-stable --cluster "$CLUSTER" --services teamagent-dev-connect-web
  # 09-24 レビュー D-10: services-stable は巻き戻りでも成功扱い。PRIMARY が新 TD で rollout が COMPLETED になるまで最大 10 分待つ
  for _ in $(seq 1 40); do
    read -r CW_TD CW_RO <<<"$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-connect-web --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition,rolloutState]' --output text)"
    [ "$CW_TD" = "$NEW_CW" ] || { echo "FATAL: connect-web の PRIMARY が新 TD でない（${CW_TD##*/}）。巻き戻りの可能性"; exit 1; }
    [ "$CW_RO" = "FAILED" ] && { echo "FATAL: connect-web の rollout が FAILED"; exit 1; }
    [ "$CW_RO" = "COMPLETED" ] && break
    sleep 15
  done
  [ "$CW_RO" = "COMPLETED" ] || { echo "FATAL: connect-web の rollout が 10 分で COMPLETED にならない（${CW_RO}）"; exit 1; }
  aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-connect-web --query 'services[0].{td:taskDefinition,running:runningCount,rollout:deployments[0].rolloutState}' --output json | sed -E 's|arn:aws:ecs:[^"]*task-definition/||g'
fi

swap_rule_target() {  # <rule> <new TD ARN>
  local rule="$1" new="$2"
  aws events list-targets-by-rule --rule "$rule" --query 'Targets' --output json > "$OUT/$rule-targets.json"
  python3 - "$OUT/$rule-targets.json" "$new" > "$OUT/$rule-targets-new.json" <<'PY'
import json, sys
targets = json.load(open(sys.argv[1])); new = sys.argv[2]
assert len(targets) == 1, f"target が 1 件でない: {len(targets)}"
t = targets[0]; old = t["EcsParameters"]["TaskDefinitionArn"]
t["EcsParameters"]["TaskDefinitionArn"] = new
print(f"  {sys.argv[1].split('/')[-1]}: {old.rsplit('/',1)[-1]} -> {new.rsplit('/',1)[-1]}", file=sys.stderr)
json.dump(targets, sys.stdout)
PY
  if [ "$DRY" = "--dry-run" ]; then echo "  (dry-run) put-targets をスキップ" >&2; return; fi
  aws --profile "$RELEASE_PROFILE" events put-targets --rule "$rule" --targets "file://$OUT/$rule-targets-new.json" --query 'FailedEntryCount' --output text | sed 's/^/  FailedEntryCount=/'
  aws events list-targets-by-rule --rule "$rule" --query 'Targets[0].EcsParameters.TaskDefinitionArn' --output text | sed -E 's|.*/|  now: |'
}

echo "=== 2. morning-digest（EventBridge target）"
NEW_MD="$(register_from teamagent-dev-morning-digest)"
swap_rule_target teamagent-dev-morning-digest-weekday "$NEW_MD"
# 2026-10-08: #496（個人別配信）の planner rule も同じ TD を使う。ここに無かったため planner だけ古い版（:88）のまま動いていた。
swap_rule_target teamagent-dev-morning-digest-planner "$NEW_MD"

echo "=== 3. canary（EventBridge target・rule は現状維持）"
NEW_CN="$(register_from teamagent-dev-canary)"
swap_rule_target teamagent-dev-canary-hourly "$NEW_CN"

echo "=== 4. ingest（dispatch Lambda の TASKDEF_ARN）"
NEW_IN="$(register_from teamagent-dev-ingest)"
aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables' --output json > "$OUT/ingest-lambda-env.json"
python3 - "$OUT/ingest-lambda-env.json" "$NEW_IN" > "$OUT/ingest-lambda-env-new.json" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])); assert "TASKDEF_ARN" in env
print(f"  TASKDEF_ARN: {env['TASKDEF_ARN'].rsplit('/',1)[-1]} -> {sys.argv[2].rsplit('/',1)[-1]}", file=sys.stderr)
env["TASKDEF_ARN"] = sys.argv[2]; json.dump({"Variables": env}, sys.stdout)
PY
if [ "$DRY" != "--dry-run" ]; then
  aws --profile "$RELEASE_PROFILE" lambda update-function-configuration --function-name teamagent-dev-ingest-dispatch --environment "file://$OUT/ingest-lambda-env-new.json" --query 'LastUpdateStatus' --output text | sed 's/^/  /'
  aws lambda wait function-updated --function-name teamagent-dev-ingest-dispatch
  aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text | sed -E 's|.*/|  now: |'
fi

echo "🏁 consumer 4 つの差し替え $( [ "$DRY" = "--dry-run" ] && echo '（dry-run・変更なし）' || echo '完了' ) image=$DIGEST"

#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# ingest 手動実行の正規経路（月次 Runbook ①・docs/runbooks/connect_web_monthly.md）。
#   (a) git 管理の data/ingest_sources.yaml を sha256 算出のうえ S3 へ配置
#   (b) ネットワーク設定を EventBridge ルールのターゲットから動的取得（tf ドリフト回避）
#   (c) ecs run-task（td は family 名指定＝最新 ACTIVE revision）＋ env 注入
#   (d) 完了待ち → exitCode 検証（非0は exit 1・fail-loud）
#   (e) CloudWatch ログ末尾の要約表示（documents/chunks・yaml sha 一致・skipped_folder）
#   タスク側は INGEST_SOURCES_S3_URI から yaml を取得し、取得失敗・sha 不一致は
#   即 exit 1（同梱 yaml への silent fallback 禁止）。image 焼き込み yaml 問題の恒久解。
#
# 使い方:
#   bash scripts/aws/run_ingest_task.sh                        # slack,gdrive,gsheets
#   bash scripts/aws/run_ingest_task.sh --sources gdrive       # Drive のみ（再編当日）
#   bash scripts/aws/run_ingest_task.sh --mark-stale           # stale soft-delete 付き
#   bash scripts/aws/run_ingest_task.sh --mark-stale --allow-mass-stale
#
# --sources のトークン（scripts/run_ingest_fargate.py → pipeline.IngestRunner.run の
# kind 分岐と同一）: slack / gdrive / gsheets / shared_drives（共有ドライブ crawl）/ all（全部）
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
BUCKET=teamagent-dev-raw-files
TD_FAMILY=teamagent-dev-ingest
CONTAINER=ingest
RULE=teamagent-dev-ingest-weekly
LOG_GROUP=/teamagent/dev/ingest
YAML_KEY=config/ingest_sources.yaml
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # scripts/aws/ から2つ上
YAML_PATH="$REPO_ROOT/data/ingest_sources.yaml"
WAIT_MAX_MIN="${WAIT_MAX_MIN:-120}"   # full crawl は長い。env で延長可

SOURCES="slack,gdrive,gsheets"
MARK_STALE=false
ALLOW_MASS=false
ROOT_WARN_ONLY=false

usage() {
  cat <<'EOF'
usage: run_ingest_task.sh [--sources <csv>] [--mark-stale] [--allow-mass-stale] [--root-check-warn-only]
  --sources           取り込み対象（カンマ区切り・既定 slack,gdrive,gsheets）
                      トークン: slack / gdrive / gsheets / shared_drives（共有ドライブ crawl）/ all
  --mark-stale        run 中に未観測だった gdrive documents へ metadata.stale を付与（soft-delete）
  --allow-mass-stale  stale 候補が既存の 50% 超でも中止せず続行（INGEST_STALE_ALLOW_MASS=true）
  --root-check-warn-only
                      ナレッジ/ 直下の yaml 未登載 NN_ フォルダによる exit 1 を WARNING に降格
                      （INGEST_ROOT_CHECK_WARN_ONLY=true を注入・既定 false・暫定用）
  env:
    SUBNETS / SECURITY_GROUPS  EventBridge ルールからネットワーク設定が取れない時の明示指定
    WAIT_MAX_MIN               完了待ちの上限分（既定 120）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --sources) SOURCES="${2:?--sources に値が必要}"; shift 2 ;;
    --mark-stale) MARK_STALE=true; shift ;;
    --allow-mass-stale) ALLOW_MASS=true; shift ;;
    --root-check-warn-only) ROOT_WARN_ONLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "★不明な引数: $1"; usage; exit 1 ;;
  esac
done

command -v jq >/dev/null || { echo "★jq が必要"; exit 1; }
command -v aws >/dev/null || { echo "★aws CLI が必要"; exit 1; }
test -f "$YAML_PATH" || { echo "★yaml が無い: $YAML_PATH"; exit 1; }

echo "== 0) preflight: ingest task role の yaml 読取 policy（無いとタスク内 S3 取得が AccessDenied → 即 exit 1）=="
TASK_ROLE_ARN=$(aws ecs describe-task-definition --region "$R" --task-definition "$TD_FAMILY" \
  --query 'taskDefinition.taskRoleArn' --output text)
[ -n "$TASK_ROLE_ARN" ] && [ "$TASK_ROLE_ARN" != "None" ] \
  || { echo "★td($TD_FAMILY) に taskRoleArn が無い"; exit 1; }
TASK_ROLE="${TASK_ROLE_ARN##*/}"
aws iam get-role-policy --role-name "$TASK_ROLE" --policy-name ingest-yaml-s3-read >/dev/null 2>&1 \
  || { echo "★task role($TASK_ROLE) に ingest-yaml-s3-read policy が無い。先に infra/deploy/bootstrap_apphtml_s3_iam.sh を1回実行せよ"; exit 1; }
echo "  OK（$TASK_ROLE に付与済）"

echo "== 1) git 管理 yaml の sha256 算出 → S3 配置 =="
YAML_SHA=$(shasum -a 256 "$YAML_PATH" | awk '{print $1}')
aws s3 cp "$YAML_PATH" "s3://$BUCKET/$YAML_KEY" --region "$R"
echo "  sha256=$YAML_SHA"

echo "== 2) ネットワーク設定を EventBridge ルール($RULE)のターゲットから取得 =="
TGT=$(aws events list-targets-by-rule --region "$R" --rule "$RULE" \
  --query 'Targets[0]' --output json 2>/dev/null || echo "null")
if [ "$TGT" != "null" ] && [ -n "$TGT" ]; then
  NETCFG=$(echo "$TGT" | jq -c '{awsvpcConfiguration: {
    subnets: .EcsParameters.NetworkConfiguration.awsvpcConfiguration.Subnets,
    securityGroups: .EcsParameters.NetworkConfiguration.awsvpcConfiguration.SecurityGroups,
    assignPublicIp: (.EcsParameters.NetworkConfiguration.awsvpcConfiguration.AssignPublicIp // "ENABLED")}}')
elif [ -n "${SUBNETS:-}" ] && [ -n "${SECURITY_GROUPS:-}" ]; then
  NETCFG=$(jq -nc --arg sn "$SUBNETS" --arg sg "$SECURITY_GROUPS" '{awsvpcConfiguration: {
    subnets: ($sn | split(",")), securityGroups: ($sg | split(",")), assignPublicIp: "ENABLED"}}')
else
  echo "★EventBridge ルール $RULE のターゲットが取れない（rule 不在 or 権限）。"
  echo "  env SUBNETS=subnet-a,subnet-b SECURITY_GROUPS=sg-x で明示指定するか terraform(ingest_schedule.tf)を確認"
  exit 1
fi
echo "$NETCFG" | jq -e '.awsvpcConfiguration.subnets | length > 0' >/dev/null \
  || { echo "★ネットワーク設定の subnets が空: $NETCFG"; exit 1; }
echo "  $NETCFG"

echo "== 3) run-task（td=$TD_FAMILY 最新 revision・containerOverrides で env 注入）=="
OVERRIDES=$(jq -nc \
  --arg c "$CONTAINER" --arg src "$SOURCES" --arg uri "s3://$BUCKET/$YAML_KEY" \
  --arg sha "$YAML_SHA" --arg ms "$MARK_STALE" --arg am "$ALLOW_MASS" --arg rw "$ROOT_WARN_ONLY" '
  {containerOverrides: [{name: $c, environment: [
    {name: "INGEST_SOURCES",              value: $src},
    {name: "INGEST_SOURCES_S3_URI",       value: $uri},
    {name: "INGEST_SOURCES_SHA256",       value: $sha},
    {name: "USE_DOC_KIND_RULES",          value: "true"},
    {name: "INGEST_MARK_STALE",           value: $ms},
    {name: "INGEST_STALE_ALLOW_MASS",     value: $am},
    {name: "INGEST_ROOT_CHECK_WARN_ONLY", value: $rw}
  ]}]}')
RUN_JSON=$(aws ecs run-task --region "$R" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$TD_FAMILY" \
  --network-configuration "$NETCFG" \
  --overrides "$OVERRIDES" --output json)
TASK_ARN=$(echo "$RUN_JSON" | jq -r '.tasks[0].taskArn // empty')
[ -n "$TASK_ARN" ] || { echo "★run-task 失敗:"; echo "$RUN_JSON" | jq '.failures'; exit 1; }
TID="${TASK_ARN##*/}"
echo "  task=$TASK_ARN"
echo "  sources=$SOURCES mark_stale=$MARK_STALE allow_mass_stale=$ALLOW_MASS root_check_warn_only=$ROOT_WARN_ONLY"
echo "  ログ追従: AWS_REGION=$R aws logs tail $LOG_GROUP --follow --since 1m"

echo "== 4) 完了待ち（最大 ${WAIT_MAX_MIN} 分・30秒間隔）=="
DEADLINE=$(( $(date +%s) + WAIT_MAX_MIN * 60 ))
while :; do
  ST=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].lastStatus' --output text)
  echo "  $(date +%H:%M:%S) lastStatus=$ST"
  [ "$ST" = "STOPPED" ] && break
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "★${WAIT_MAX_MIN}分を超過（タスクは走り続けている）。ログ: aws logs tail $LOG_GROUP --follow"
    exit 1
  fi
  sleep 30
done
EXIT_CODE=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
STOP_REASON=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].stoppedReason' --output text)
echo "  exitCode=$EXIT_CODE stoppedReason=$STOP_REASON"

echo "== 5) CloudWatch ログ末尾の要約 =="
STREAM="ingest/$CONTAINER/$TID"
LOG=$(aws logs get-log-events --region "$R" --log-group-name "$LOG_GROUP" \
  --log-stream-name "$STREAM" --limit 500 \
  --query 'events[].message' --output text 2>/dev/null || true)
if [ -n "$LOG" ]; then
  echo "$LOG" | tr '\t' '\n' | grep -E 'Ingest Result|documents=|chunks=|ingest_sources|skipped_folder|stale' | tail -40 \
    || echo "  （要約行なし。全文: aws logs tail $LOG_GROUP --since 3h）"
  # タスク側の成功ログ（run_ingest_fargate.py / loader.py）は sha256 を先頭12hexに
  # 短縮して出力するため、full 64hex ではなく12hex接頭辞で照合する。
  if echo "$LOG" | grep -q "${YAML_SHA:0:12}"; then
    echo "  ✅ yaml sha256 の一致をログで確認（先頭12hex=${YAML_SHA:0:12} / full=${YAML_SHA}）"
  else
    echo "  ⚠ ログに yaml sha256（先頭12hex=${YAML_SHA:0:12}）が見つからない。ingest_sources ログを目視確認せよ"
  fi
else
  echo "  ⚠ ログ取得失敗（stream=${STREAM}）。aws logs tail $LOG_GROUP --since 3h で確認"
fi

if [ "$EXIT_CODE" != "0" ]; then
  echo "★ingest 失敗（exitCode=${EXIT_CODE}）。上のログと docs/runbooks/connect_web_monthly.md のトラブルシュートを参照"
  echo "  （stale ブレーキ 50% 超で中止した場合は --allow-mass-stale で明示続行）"
  exit 1
fi
echo "✅ ingest 完了。次工程（export_vault → build_app_html → publish）は Runbook 参照"

#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# connect-web 統合タスク昇格 = HMAC rollout の正規経路（git管理）。
#   事前ビルド・review済みのdigest固定イメージを1タスク定義へ「宣言的に」載せる:
#     (1) /app Obsidian風UI（機密app.htmlはgit非搭載でCodeBuild時にS3から注入）
#     (2) 全社ログイン CONNECT_SEARCH_ALLOWED_HD=vectorinc.co.jp（会社ドメイン開放）
#     (3) Slack個人連携(#156) SLACK_OAUTH_REDIRECT_URI + Slack secrets 3本
#   このスクリプトはHTML/sourceのS3 publish、CodeBuild、Vault exportを行わない。
#   terraform 非経由（ECS直・ドリフト回避）。env/secrets は毎回 select除去→再付与のフルセット
#   （base td 継承の"たまたま残る/重複する"を排除。過去の Duplicate secret 事故を根絶）。
#   ⚠️ 旧スクリプト(redeploy_app.sh / teamagent-launch の redeploy_connectweb.sh 等)は使わない。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-connect-web
TD_FAMILY=teamagent-dev-connect-web
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # infra/deploy/ から2つ上
HD=vectorinc.co.jp
SLACK_REDIRECT="https://connect.newstv.co.jp/slack/oauth/callback"   # サービスhost=newstv.co.jp（loginのhd=vectorinc.co.jpとは別物・両方正しい）
CID_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_client_id-aTZTb2"
CSEC_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_secret-fOlJIt"
CSTATE_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/slack_oauth_state_secret-yGYkUF"
APP_HTML_URI="${HMAC_CONNECT_APP_HTML_S3_URI:-s3://teamagent-dev-raw-files/codebuild/connect-web-app.html}"
HMAC_PREFLIGHT_MANIFEST="${HMAC_PREFLIGHT_MANIFEST:-}"
HMAC_ROLLOUT_CONTROL="${HMAC_ROLLOUT_CONTROL:-}"
HMAC_CONNECT_TASK_TEMPLATE="${HMAC_CONNECT_TASK_TEMPLATE:-}"
HMAC_CONNECT_PHASE="${HMAC_CONNECT_PHASE:-}"
HMAC_CONNECT_IMAGE="${HMAC_CONNECT_IMAGE:-}"
HMAC_CONNECT_MODE="${HMAC_CONNECT_MODE:-candidate}"
HMAC_REGISTERED_TASK_ARN="${HMAC_REGISTERED_TASK_ARN:-}"
PREFLIGHT_PY="${PREFLIGHT_PY:-$REPO_ROOT/.venv/bin/python}"

command -v jq >/dev/null || { echo "jq が必要"; exit 1; }
test -n "$HMAC_PREFLIGHT_MANIFEST" || {
  echo "★ HMAC_PREFLIGHT_MANIFEST（secret-free reviewed JSON）が必須。旧DB-primary taskの複製を拒否。" >&2
  exit 2
}
test -f "$HMAC_PREFLIGHT_MANIFEST" || { echo "★ HMAC preflight manifest が読めない" >&2; exit 2; }
test -n "$HMAC_ROLLOUT_CONTROL" && test -f "$HMAC_ROLLOUT_CONTROL" || {
  echo "★ HMAC_ROLLOUT_CONTROL（secret-free live control JSON）が必須" >&2
  exit 2
}
case "$HMAC_CONNECT_MODE" in
  candidate|cleanup)
    test -n "$HMAC_CONNECT_TASK_TEMPLATE" && test -f "$HMAC_CONNECT_TASK_TEMPLATE" || {
      echo "★ HMAC_CONNECT_TASK_TEMPLATE（reviewed full task JSON）が必須" >&2
      exit 2
    }
    [[ "$HMAC_CONNECT_IMAGE" =~ ^[^[:space:]@]+@sha256:[a-f0-9]{64}$ ]] || {
      echo "★ HMAC_CONNECT_IMAGE は事前ビルド・review済みのdigest固定URIが必須" >&2
      exit 2
    }
    if [[ "$HMAC_CONNECT_MODE" == "candidate" ]]; then
      case "$HMAC_CONNECT_PHASE" in
        preload) LIVE_GATE_ACTION=pre-connect-preload ;;
        final) LIVE_GATE_ACTION=pre-connect-final ;;
        *) echo "★ HMAC_CONNECT_PHASE は preload または final が必須" >&2; exit 2 ;;
      esac
    fi
    ;;
  rollback)
    [[ "$HMAC_REGISTERED_TASK_ARN" =~ :task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$ ]] || {
      echo "★ rollback mode requires the exact approved task definition ARN" >&2
      exit 2
    }
    ;;
  *)
    echo "★ HMAC_CONNECT_MODE は candidate、cleanup、または rollback が必須" >&2
    exit 2
    ;;
esac
test -x "$PREFLIGHT_PY" || { echo "★ preflight Python が実行できない: $PREFLIGHT_PY" >&2; exit 2; }
"$PREFLIGHT_PY" "$REPO_ROOT/scripts/preflight_hmac_rotation.py" \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now
if [[ "$HMAC_CONNECT_MODE" == "rollback" ]]; then
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action pre-update \
    --mode rollback \
    --task connect_web \
    --task-definition-arn "$HMAC_REGISTERED_TASK_ARN"
  aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" \
    --task-definition "$HMAC_REGISTERED_TASK_ARN" >/dev/null
  aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action post-update \
    --mode rollback \
    --task connect_web \
    --task-definition-arn "$HMAC_REGISTERED_TASK_ARN"
  echo "hmac_connect_rollback=true exact_artifact=true"
  exit 0
fi
if [[ "$HMAC_CONNECT_MODE" == "candidate" ]]; then
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action "$LIVE_GATE_ACTION"
fi

echo "== 0) preflight: exec-role の Slack secret 読取policyを確認（無いと統合td起動がGetSecretValue AccessDeniedで自動ロールバック） =="
aws iam get-role-policy --role-name teamagent-dev-ecs-exec-connect-web --policy-name slack-oauth-secrets >/dev/null 2>&1 \
  || { echo "★ exec-role(teamagent-dev-ecs-exec-connect-web) に slack-oauth-secrets policy が無い。先に infra/deploy/bootstrap_slack_iam.sh を1回実行せよ"; exit 1; }
echo "  OK（付与済）"

echo "== 1) review済みimmutable image =="
IMG="$HMAC_CONNECT_IMAGE"
echo "  digest_pinned=true"

echo "== 2) 現行td取得（ロールバックARN控え） =="
aws ecs describe-task-definition --region "$R" --task-definition "$TD_FAMILY" --query 'taskDefinition' > /tmp/cwu_td.json
CUR_ARN=$(jq -r '.taskDefinitionArn' /tmp/cwu_td.json); echo "  現行観測task: $CUR_ARN"
jq '
  if has("taskDefinition") then .taskDefinition else . end
  | del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredAt,.registeredBy,.deregisteredAt)
' "$HMAC_CONNECT_TASK_TEMPLATE" > /tmp/cwu_base.json

echo "== 3) 新td生成（宣言的フルセット: image + ALLOWED_HD + app.html S3 URI + No-AI フラグ + Slack env/secrets を毎回 除去→再付与） =="
# CONNECT_APP_HTML_S3_URI: /app ホットスワップ（publish_app_html.sh）の受け口。
#   publish script の配置先（s3://$BUCKET/codebuild/connect-web-app.html）と同一定数。
# USE_QUERY_PLANNER/USE_COHERE_RERANK=false: T1 No-AI 化の恒久化
#   （runtime td 手術で反映済みの変更が bake で巻き戻らないよう宣言的に固定）。
jq --arg img "$IMG" --arg hd "$HD" --arg rd "$SLACK_REDIRECT" --arg apphtml "$APP_HTML_URI" \
   --arg cid "$CID_ARN" --arg csec "$CSEC_ARN" --arg cst "$CSTATE_ARN" '
  .containerDefinitions[0].image=$img
  | .containerDefinitions[0].environment=(
      [.containerDefinitions[0].environment[]|select(.name!="CONNECT_SEARCH_ALLOWED_HD" and .name!="SLACK_OAUTH_REDIRECT_URI"
        and .name!="CONNECT_APP_HTML_S3_URI" and .name!="USE_QUERY_PLANNER" and .name!="USE_COHERE_RERANK")]
      + [{"name":"CONNECT_SEARCH_ALLOWED_HD","value":$hd},{"name":"SLACK_OAUTH_REDIRECT_URI","value":$rd},
         {"name":"CONNECT_APP_HTML_S3_URI","value":$apphtml},
         {"name":"USE_QUERY_PLANNER","value":"false"},{"name":"USE_COHERE_RERANK","value":"false"}])
  | .containerDefinitions[0].secrets=(
      [((.containerDefinitions[0].secrets)//[])[]|select(.name!="CONNECT_SLACK_CLIENT_ID" and .name!="CONNECT_SLACK_CLIENT_SECRET" and .name!="SLACK_OAUTH_STATE_SECRET")]
      + [{"name":"CONNECT_SLACK_CLIENT_ID","valueFrom":$cid},{"name":"CONNECT_SLACK_CLIENT_SECRET","valueFrom":$csec},{"name":"SLACK_OAUTH_STATE_SECRET","valueFrom":$cst}])
  | del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredAt,.registeredBy,.deregisteredAt)
' /tmp/cwu_base.json > /tmp/cwu_new.json
"$PREFLIGHT_PY" "$REPO_ROOT/scripts/preflight_hmac_rotation.py" \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --task-definition-json connect_web=/tmp/cwu_new.json
"$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --control "$HMAC_ROLLOUT_CONTROL" \
  --action pre-register \
  --mode "$HMAC_CONNECT_MODE" \
  --task connect_web \
  --task-definition-json /tmp/cwu_new.json
NEW_ARN=$(aws ecs register-task-definition --region "$R" --cli-input-json file:///tmp/cwu_new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo "  新リビジョン: $NEW_ARN"

echo "== 4) update-service → 安定待ち =="
"$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --control "$HMAC_ROLLOUT_CONTROL" \
  --action pre-update \
  --mode "$HMAC_CONNECT_MODE" \
  --task connect_web \
  --task-definition-arn "$NEW_ARN"
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" --task-definition "$NEW_ARN" >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
if [[ "$HMAC_CONNECT_MODE" == "cleanup" ]]; then
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action post-update \
    --mode cleanup \
    --task connect_web \
    --task-definition-arn "$NEW_ARN"
elif [[ "$HMAC_CONNECT_PHASE" == "preload" ]]; then
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action connect-web-preloaded
else
  "$PREFLIGHT_PY" "$REPO_ROOT/scripts/hmac_rollout_gate.py" \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action complete
fi
echo ""
echo "✅ 統合デプロイ完了。3機能同居:"
echo "   /app（Obsidian UI・実HTML）/ /search（303）/ /slack/oauth/callback（Slack個人連携）"
echo "   image_digest_pinned=true"
echo "   検証: https://connect.newstv.co.jp/app を @vectorinc.co.jp でログイン（/appが\"準備中\"でなく実UIか確認）"
echo "⏪ ロールバックは HMAC_ROLLOUT_CONTROL に事前登録・検証済みの専用taskだけを使用（現行td53への復帰禁止）。"

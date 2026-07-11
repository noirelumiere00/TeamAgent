#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# connect-web 統合デプロイ = 唯一の正規経路（git管理）。
#   3機能を1イメージ・1タスク定義に「宣言的に」載せる:
#     (1) /app Obsidian風UI（機密app.htmlはgit非搭載でCodeBuild時にS3から注入）
#     (2) 全社ログイン CONNECT_SEARCH_ALLOWED_HD=vectorinc.co.jp（会社ドメイン開放）
#     (3) Slack個人連携(#156) SLACK_OAUTH_REDIRECT_URI + Slack secrets 3本
#   terraform 非経由（ECS直・ドリフト回避）。env/secrets は毎回 select除去→再付与のフルセット
#   （base td 継承の"たまたま残る/重複する"を排除。過去の Duplicate secret 事故を根絶）。
#   ⚠️ 旧スクリプト(redeploy_app.sh / teamagent-launch の redeploy_connectweb.sh 等)は使わない。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-connect-web
TD_FAMILY=teamagent-dev-connect-web
ECR="718959508629.dkr.ecr.$R.amazonaws.com/teamagent-mcp"
BUCKET=teamagent-dev-raw-files
CB_PROJECT=teamagent-dev-image-builder
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # infra/deploy/ から2つ上
SRC_HTML="${SRC_HTML:-$HOME/Documents/Claude/Artifacts/connect-web-obsidian-preview.html}"
DST_HTML="$REPO_ROOT/src/teamagent/connect_web/static/app.html"
HD=vectorinc.co.jp
SLACK_REDIRECT="https://connect.newstv.co.jp/slack/oauth/callback"   # サービスhost=newstv.co.jp（loginのhd=vectorinc.co.jpとは別物・両方正しい）
CID_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_client_id-aTZTb2"
CSEC_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_secret-fOlJIt"
CSTATE_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/slack_oauth_state_secret-yGYkUF"
TAG="connect-unified-$(date +%Y%m%d-%H%M%S)"

command -v jq >/dev/null || { echo "jq が必要"; exit 1; }

echo "== 0) preflight: exec-role の Slack secret 読取policyを確認（無いと統合td起動がGetSecretValue AccessDeniedで自動ロールバック） =="
aws iam get-role-policy --role-name teamagent-dev-ecs-exec-connect-web --policy-name slack-oauth-secrets >/dev/null 2>&1 \
  || { echo "★ exec-role(teamagent-dev-ecs-exec-connect-web) に slack-oauth-secrets policy が無い。先に infra/deploy/bootstrap_slack_iam.sh を1回実行せよ"; exit 1; }
echo "  OK（付与済）"

echo "== 1) 最新の機密HTMLを S3(codebuild/) へ配置 + repo static へ（S3=真の格納先 / zip同梱=belt&suspenders） =="
test -f "$SRC_HTML" || { echo "★生成HTMLが無い: $SRC_HTML（生成器 connect-web-obsidian_build.py で作れ）"; exit 1; }
mkdir -p "$(dirname "$DST_HTML")"; cp "$SRC_HTML" "$DST_HTML"
aws s3 cp "$DST_HTML" "s3://$BUCKET/codebuild/connect-web-app.html" --region "$R"
echo "  $(du -h "$DST_HTML" | cut -f1) -> s3://$BUCKET/codebuild/connect-web-app.html + static/app.html"

echo "== 2) source.zip -> S3（.env/secrets/pem 除外・app.html は同梱） =="
cd "$REPO_ROOT"
ZIP=/tmp/connectweb_$TAG.zip; rm -f "$ZIP"
zip -rq "$ZIP" . -x '.git/*' -x '*/__pycache__/*' -x '*.pyc' -x 'infra/terraform/.terraform/*' \
  -x 'infra/terraform/*.tfstate*' -x 'infra/terraform/*.tfplan' -x '*/node_modules/*' -x '.venv/*' -x '.pytest_cache/*' \
  -x '.env' -x '*/.env' -x 'secrets/*' -x '*/secrets/*' -x '*.pem'
aws s3 cp "$ZIP" "s3://$BUCKET/codebuild/source.zip" --region "$R"

echo "== 3) CodeBuild（git管理 infra/codebuild/buildspec.yml でS3からhtml注入）→ 完了待ち（torch入りで10-20分） =="
BID=$(aws codebuild start-build --region "$R" --project-name "$CB_PROJECT" \
  --buildspec-override infra/codebuild/buildspec.yml \
  --environment-variables-override "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" --query 'build.id' --output text)
echo "  build id: $BID"
while :; do ST=$(aws codebuild batch-get-builds --region "$R" --ids "$BID" --query 'builds[0].buildStatus' --output text)
  echo "  build: $ST"; case "$ST" in SUCCEEDED) break;; FAILED|FAULT|STOPPED|TIMED_OUT) echo "★ビルド失敗($ST)。CloudWatch /aws/codebuild/$CB_PROJECT を確認"; exit 1;; *) sleep 20;; esac; done

echo "== 4) digest取得 =="
DIGEST=$(aws ecr describe-images --region "$R" --repository-name teamagent-mcp --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
IMG="$ECR@$DIGEST"; echo "  $IMG"

echo "== 5) 現行td取得（ロールバックARN控え） =="
aws ecs describe-task-definition --region "$R" --task-definition "$TD_FAMILY" --query 'taskDefinition' > /tmp/cwu_td.json
CUR_ARN=$(jq -r '.taskDefinitionArn' /tmp/cwu_td.json); echo "  ロールバック先: $CUR_ARN"

echo "== 6) 新td生成（宣言的フルセット: image + ALLOWED_HD + app.html S3 URI + No-AI フラグ + Slack env/secrets を毎回 除去→再付与） =="
# CONNECT_APP_HTML_S3_URI: /app ホットスワップ（publish_app_html.sh）の受け口。
#   publish script の配置先（s3://$BUCKET/codebuild/connect-web-app.html）と同一定数。
# USE_QUERY_PLANNER/USE_COHERE_RERANK=false: T1 No-AI 化の恒久化
#   （runtime td 手術で反映済みの変更が bake で巻き戻らないよう宣言的に固定）。
APP_HTML_URI="s3://$BUCKET/codebuild/connect-web-app.html"
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
' /tmp/cwu_td.json > /tmp/cwu_new.json
NEW_ARN=$(aws ecs register-task-definition --region "$R" --cli-input-json file:///tmp/cwu_new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo "  新リビジョン: $NEW_ARN"

echo "== 7) update-service → 安定待ち =="
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" --task-definition "$NEW_ARN" >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
echo ""
echo "✅ 統合デプロイ完了。3機能同居:"
echo "   /app（Obsidian UI・実HTML）/ /search（303）/ /slack/oauth/callback（Slack個人連携）"
echo "   image tag: $TAG（ingest td 配布: bash infra/deploy/register_ingest_td.sh --image-tag $TAG）"
echo "   検証: https://connect.newstv.co.jp/app を @vectorinc.co.jp でログイン（/appが\"準備中\"でなく実UIか確認）"
echo "⏪ ロールバック: aws ecs update-service --region $R --cluster $CLUSTER --service $SVC --task-definition $CUR_ARN"

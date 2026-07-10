#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# /app コンテンツ更新の正規経路（月次 Runbook ③・約3分）。
#   生成済み app.html を S3 (codebuild/connect-web-app.html) へ配置し
#   force-new-deployment → 新タスクが起動後の初回アクセス時に S3 から取得して配信
#   （app.py の CONNECT_APP_HTML_S3_URI ホットスワップ）。最後に /healthz の
#   app_html_sha256 / app_html_source でローカルと配信中の一致を検証する（fail-loud）。
#   ⚠️ CodeBuild bake（10-20分）はコード変更時専用（deploy_connectweb_unified.sh）に退役。
#      コンテンツ更新でこのスクリプト以外を使わない。
# 使い方: bash infra/deploy/publish_app_html.sh [--src <生成済みhtml>]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-connect-web
BUCKET=teamagent-dev-raw-files
KEY=codebuild/connect-web-app.html
BASE_URL="${CONNECT_BASE_URL:-https://connect.newstv.co.jp}"
SRC="$HOME/Documents/Claude/Artifacts/connect-web-obsidian-preview.html"

usage() {
  cat <<'EOF'
usage: publish_app_html.sh [--src <html>]
  --src  配信する生成済み app.html（既定 ~/Documents/Claude/Artifacts/connect-web-obsidian-preview.html）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="${2:?--src に値が必要}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "★不明な引数: $1"; usage; exit 1 ;;
  esac
done

command -v jq >/dev/null || { echo "★jq が必要"; exit 1; }
command -v aws >/dev/null || { echo "★aws CLI が必要"; exit 1; }
command -v curl >/dev/null || { echo "★curl が必要"; exit 1; }

echo "== 0) preflight: live td の taskRole に S3 読取 policy ＋ CONNECT_APP_HTML_S3_URI 設定 =="
TD_ARN=$(aws ecs describe-services --region "$R" --cluster "$CLUSTER" --services "$SVC" \
  --query 'services[0].taskDefinition' --output text)
[ -n "$TD_ARN" ] && [ "$TD_ARN" != "None" ] || { echo "★service($SVC) の live td が取れない"; exit 1; }
ROLE_ARN=$(aws ecs describe-task-definition --region "$R" --task-definition "$TD_ARN" \
  --query 'taskDefinition.taskRoleArn' --output text)
[ -n "$ROLE_ARN" ] && [ "$ROLE_ARN" != "None" ] || { echo "★live td に taskRoleArn が無い"; exit 1; }
ROLE_NAME="${ROLE_ARN##*/}"
aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name apphtml-s3-read >/dev/null 2>&1 \
  || { echo "★task role($ROLE_NAME) に apphtml-s3-read policy が無い。先に infra/deploy/bootstrap_apphtml_s3_iam.sh を1回実行せよ"; exit 1; }
ENV_URI=$(aws ecs describe-task-definition --region "$R" --task-definition "$TD_ARN" \
  --query "taskDefinition.containerDefinitions[0].environment[?name=='CONNECT_APP_HTML_S3_URI'].value" --output text)
if [ -z "$ENV_URI" ] || [ "$ENV_URI" = "None" ]; then
  echo "★live td に CONNECT_APP_HTML_S3_URI が未設定（ホットスワップ非対応の旧 td）。"
  echo "  移行前日の手順（Runbook「移行手順」）どおり、対応 image + env の td を先にデプロイせよ"
  exit 1
fi
if [ "$ENV_URI" != "s3://$BUCKET/$KEY" ]; then
  echo "★live td の CONNECT_APP_HTML_S3_URI($ENV_URI) が配置先(s3://$BUCKET/$KEY)と不一致"
  exit 1
fi
echo "  OK（role=$ROLE_NAME・URI 一致）"

echo "== 1) ローカル生成 HTML の sha256 =="
test -s "$SRC" || { echo "★生成 HTML が無い/空: $SRC（scripts/build_app_html.py で生成）"; exit 1; }
LOCAL_SHA=$(shasum -a 256 "$SRC" | awk '{print $1}')
SHA12="${LOCAL_SHA:0:12}"
echo "  $(du -h "$SRC" | cut -f1)  sha256=$LOCAL_SHA（先頭12=$SHA12）"

echo "== 2) S3 へ配置 =="
aws s3 cp "$SRC" "s3://$BUCKET/$KEY" --region "$R"

echo "== 3) force-new-deployment → 安定待ち（約3分）=="
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" --force-new-deployment >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
echo "  services-stable"

echo "== 4) /healthz 検証（配信中 sha == ローカル sha ＋ source=s3）=="
OK=false
GOT_SHA=""
GOT_SRC=""
for i in 1 2 3 4 5; do
  HZ=$(curl -fsS --max-time 10 "$BASE_URL/healthz" 2>/dev/null || echo '{}')
  GOT_SHA=$(echo "$HZ" | jq -r '.app_html_sha256 // empty')
  GOT_SRC=$(echo "$HZ" | jq -r '.app_html_source // empty')
  if [ "$GOT_SRC" = "s3" ] && [ "$GOT_SHA" = "$SHA12" ]; then OK=true; break; fi
  echo "  retry $i/5: source=${GOT_SRC:-?} sha=${GOT_SHA:-?}（期待 source=s3 sha=$SHA12）"
  sleep 10
done
if [ "$OK" != "true" ]; then
  cat <<EOF
★healthz 検証失敗（配信中の app.html がこの publish と一致しない）: source=${GOT_SRC:-?} sha=${GOT_SHA:-?}
  - source=baked  … タスクが S3 取得に失敗しイメージ同梱版へフォールバック中
                    （bootstrap_apphtml_s3_iam.sh 実行済みか / S3 オブジェクト有無 / td の URI を確認）
  - source=missing … baked も無い異常。直近のコードデプロイを確認
  - sha 不一致    … 旧タスクの draining 残り等。数分待って curl -s $BASE_URL/healthz を再確認
EOF
  exit 1
fi
echo ""
echo "✅ publish 完了: /app は sha=$SHA12（source=s3）を配信中"
echo "   検証: $BASE_URL/app を @vectorinc.co.jp でログインし、フッタの更新日を目視確認"
echo "⏪ ロールバック（S3 バージョン戻し → 再デプロイ）:"
echo "   aws s3api list-object-versions --bucket $BUCKET --prefix $KEY \\"
echo "     --query 'Versions[].{v:VersionId,t:LastModified,latest:IsLatest}'"
echo "   aws s3api copy-object --bucket $BUCKET --key $KEY --copy-source \"$BUCKET/$KEY?versionId=<旧VersionId>\""
echo "   aws ecs update-service --region $R --cluster $CLUSTER --service $SVC --force-new-deployment"
echo "   （bucket versioning が無効なら、手元の前回 HTML を --src 指定で再 publish）"

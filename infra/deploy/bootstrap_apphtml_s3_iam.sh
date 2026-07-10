#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 一度きり・冪等: task role 2本へ S3 読取(GetObject)を付与（bootstrap_slack_iam.sh と同流儀）。
#   (1) connect-web task role → codebuild/connect-web-app.html（/app ホットスワップ用）
#   (2) ingest task role      → config/ingest_sources.yaml（yaml S3 オーバーライド用）
#   これが無いと S3 取得が AccessDenied になる: connect-web は baked へフォールバック
#   （healthz の app_html_source=baked で検知）、ingest は fail-loud で即 exit 1。
#   put-role-policy は冪等（同名 policy を上書き）なので何度実行しても安全。
#   ※terraform 非経由（ドリフト回避）。将来 tf 側に取り込む時は connect_web.tf / ingest_schedule.tf へ。
# 使い方: bash infra/deploy/bootstrap_apphtml_s3_iam.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
BUCKET=teamagent-dev-raw-files
CW_TD=teamagent-dev-connect-web
INGEST_TD=teamagent-dev-ingest

command -v aws >/dev/null || { echo "★aws CLI が必要"; exit 1; }

# td(family) の live taskRoleArn から role 名を解決する（ハードコードせず実体に追従）
role_name_of() {
  local td="$1" arn
  arn=$(aws ecs describe-task-definition --region "$R" --task-definition "$td" \
    --query 'taskDefinition.taskRoleArn' --output text)
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    echo ""
  else
    echo "${arn##*/}"
  fi
}

echo "== 1) connect-web task role: apphtml-s3-read（/app ホットスワップの読取）=="
CW_ROLE=$(role_name_of "$CW_TD")
[ -n "$CW_ROLE" ] || { echo "★td($CW_TD) に taskRoleArn が無い"; exit 1; }
aws iam put-role-policy --role-name "$CW_ROLE" --policy-name apphtml-s3-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/codebuild/connect-web-app.html\"}]}"
echo "  OK（$CW_ROLE）"

echo "== 2) ingest task role: ingest-yaml-s3-read（yaml オーバーライドの読取）=="
INGEST_ROLE=$(role_name_of "$INGEST_TD")
[ -n "$INGEST_ROLE" ] || { echo "★td($INGEST_TD) に taskRoleArn が無い"; exit 1; }
aws iam put-role-policy --role-name "$INGEST_ROLE" --policy-name ingest-yaml-s3-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/config/ingest_sources.yaml\"}]}"
echo "  OK（$INGEST_ROLE）"

echo "✅ 完了（冪等・以後不要）。"

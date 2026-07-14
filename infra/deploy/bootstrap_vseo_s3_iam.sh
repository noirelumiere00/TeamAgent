#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 一度きり・冪等: connect-web task role へ S3 読取(GetObject)を付与（apphtml と同流儀）。
#   connect-web の /r/<token> がレポートの presigned を「都度再生成」して 302 する。
#   presigned はローカル署名だが、URL は署名プリンシパル(=connect-web task role)に対象keyの
#   GetObject 権限が無いとブラウザ取得時に 403 になる。これを付与する。
#   対象 prefix: vseo-reports/（x_research 等のカード集HTML）・vseo-proposals/（提案 PPTX/PDF）。
#   put-role-policy は冪等（同名 policy を上書き）なので何度実行しても安全。
#   ※terraform 非経由（別名 inline policy＝apply で剥がれない）。tf 側の真実源は
#     connect_web.tf の VseoReportS3Read statement（apply 取込時はこの inline と重複しても無害）。
# 使い方: bash infra/deploy/bootstrap_vseo_s3_iam.sh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
R=ap-northeast-1
BUCKET=teamagent-dev-raw-files
CW_TD=teamagent-dev-connect-web

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

echo "== connect-web task role: vseo-s3-read（/r 短縮リンクの presigned 再生成用 GetObject）=="
CW_ROLE=$(role_name_of "$CW_TD")
[ -n "$CW_ROLE" ] || { echo "★td($CW_TD) に taskRoleArn が無い"; exit 1; }
aws iam put-role-policy --role-name "$CW_ROLE" --policy-name vseo-s3-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$BUCKET/vseo-reports/*\",\"arn:aws:s3:::$BUCKET/vseo-proposals/*\"]}]}"
echo "  OK（${CW_ROLE}）"

echo "✅ 完了（冪等・以後不要）。"

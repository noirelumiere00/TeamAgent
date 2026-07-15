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

# ── 検証: connect-web role が両 prefix を GetObject 認可されるか実証する ────────────
# 「/r→302」は IAM 成功を**証明しない**: presign(generate_presigned_url) はローカル署名操作で、
# 対象 key の GetObject 権限が無くても 302 は返る（実際の GetObject はブラウザが 302 を追って
# S3 に当たった時に初めて評価され、権限が無ければ 403 に劣化する）。put-role-policy の反映を
# simulate-principal-policy で実プリンシパル（connect-web task role）に対し評価し、
# 両 prefix で allowed を確認できなければ fail-close する（ここで落ちれば ON にしてはいけない）。
# 注意: simulate は **アイデンティティポリシーのみ**を評価する。バケットポリシーの Deny・
# SSE-KMS の kms:Decrypt・SCP・permissions boundary は評価しない（現状 raw_files は SSE-S3・
# バケットポリシー無し・制限SCP無しなので乖離しないが、将来 KMS 化/Deny 追加時は allowed でも
# 実取得 403 になりうる）。そのため最終判定は下記の実機 /r 追従 200 で担保する。
CW_ROLE_ARN=$(aws ecs describe-task-definition --region "$R" --task-definition "$CW_TD" \
  --query 'taskDefinition.taskRoleArn' --output text)
echo "== 検証: task role の GetObject 認可（302 ではなく IAM 評価で実証）=="
verify_get() {
  local resource="$1" label="$2" decision
  decision=$(aws iam simulate-principal-policy \
    --policy-source-arn "$CW_ROLE_ARN" \
    --action-names s3:GetObject \
    --resource-arns "$resource" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "error")
  if [ "$decision" = "allowed" ]; then
    echo "  ✅ ${label}: allowed"
  else
    echo "  ❌ ${label}: ${decision}（GetObject 不可＝/r は 403 に劣化。USE_REPORT_SHORTURL を ON にしないこと）"
    exit 1
  fi
}
verify_get "arn:aws:s3:::$BUCKET/vseo-reports/_probe.html" "vseo-reports/"
verify_get "arn:aws:s3:::$BUCKET/vseo-proposals/_probe.pptx" "vseo-proposals/"

echo "✅ 完了（冪等・以後不要）。IAM 認可を実証済み。"
echo "ℹ️  最終フリップ判定は実機 /r をリダイレクト追従して 200 を確認する:"
echo "    curl -sSL -o /dev/null -w '%{http_code}\\n' \"\$CONNECT_BASE_URL/r/<token>\"  # 302 ではなく最終 200"

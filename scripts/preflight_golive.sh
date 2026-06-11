#!/usr/bin/env bash
# §P: go-live プリフライト（read-only・何も変更しない）。
# deploy_runbook.md §0-§2 を始める前に、環境の準備不足を一括で洗い出す。
# 全チェックを fail-soft で実行し、最後に PASS/FAIL/SKIP を要約する（exit 0=全PASS, 1=FAILあり, 2=SKIPのみ欠け）。
#
# Usage:  bash scripts/preflight_golive.sh            # P1 薄殻（4ナレッジツール）
#         WITH_SCRAPE=1 bash scripts/preflight_golive.sh   # 拡張版（gemini secret も確認）
#
# 検査対象（変更は一切しない）:
#   1) AWS 認証/アカウント(718959508629)/リージョン(ap-northeast-1)
#   2) Bedrock Haiku4.5 推論プロファイル実ID（config/tfvars に入れる値の確定）
#   3) Secrets Manager 5本（variables_fargate.tf の default 名）
#   4) terraform CLI / S3 backend 到達（validate は社内proxyで失敗し得る＝SKIP扱い）
#   5) docker buildx（arm64 ビルド用）

set -u
R="ap-northeast-1"
ACCOUNT_EXPECTED="718959508629"
PASS=(); FAIL=(); SKIP=()
ok()   { PASS+=("$1"); printf '[PASS] %s\n' "$1"; }
ng()   { FAIL+=("$1: $2"); printf '[FAIL] %s — %s\n' "$1" "$2"; }
skip() { SKIP+=("$1: $2"); printf '[SKIP] %s — %s\n' "$1" "$2"; }

echo "=== TeamAgent go-live preflight (read-only / region=$R) ==="

# 0) aws CLI
if ! command -v aws >/dev/null 2>&1; then
  skip "aws-cli" "aws コマンドが無い（インストール後に再実行）"
  CREDS=0
else
  # 1) 認証・アカウント
  if IDENT=$(aws sts get-caller-identity --output json 2>&1); then
    ACCT=$(printf '%s' "$IDENT" | grep -o '"Account": *"[0-9]*"' | grep -o '[0-9]*')
    if [ "$ACCT" = "$ACCOUNT_EXPECTED" ]; then
      ok "aws-identity (account=$ACCT)"
    else
      ng "aws-identity" "account=$ACCT ≠ 期待 $ACCOUNT_EXPECTED（プロファイル違い？）"
    fi
    CREDS=1
  else
    skip "aws-identity" "認証情報なし/期限切れ（aws sso login 等の後に再実行）"
    CREDS=0
  fi
fi

if [ "${CREDS:-0}" = "1" ]; then
  # 2) Bedrock 推論プロファイル（Haiku4.5 の実ID＝config/tfvars に入れる値）
  if PROFILES=$(aws bedrock list-inference-profiles --region "$R" --output json 2>&1); then
    HAIKU=$(printf '%s' "$PROFILES" | grep -o '"inferenceProfileId": *"[^"]*haiku[^"]*"' | head -3 || true)
    if [ -n "$HAIKU" ]; then
      ok "bedrock-haiku-profile"
      printf '       → config/tfvars に入れる実ID候補:\n%s\n' "$HAIKU" | sed 's/"inferenceProfileId": */         /'
    else
      ng "bedrock-haiku-profile" "haiku を含む推論プロファイルが見つからない（モデルアクセス申請/リージョン確認）"
    fi
  else
    ng "bedrock-list" "list-inference-profiles 失敗（IAM 権限 bedrock:ListInferenceProfiles）"
  fi

  # 3) Secrets（runbook §1 で作る5本。存在しない＝§1 が未実施なだけなので INFO 扱いの FAIL ではなく明示）
  SECRETS="teamagent/dev/mcp/bearer teamagent/dev/database-url teamagent/dev/openclaw/slack-bot-token teamagent/dev/openclaw/slack-app-token teamagent/dev/openclaw/gateway-token"
  [ "${WITH_SCRAPE:-0}" = "1" ] && SECRETS="$SECRETS teamagent/dev/gemini-api-key"
  for s in $SECRETS; do
    if aws secretsmanager describe-secret --region "$R" --secret-id "$s" >/dev/null 2>&1; then
      ok "secret:$s"
    else
      ng "secret:$s" "未作成（runbook §1 を実施）"
    fi
  done
fi

# 4) terraform
if command -v terraform >/dev/null 2>&1; then
  ok "terraform-cli ($(terraform version -json 2>/dev/null | grep -o '"terraform_version": *"[^"]*"' | cut -d'"' -f4 || terraform version | head -1))"
  if [ "${CREDS:-0}" = "1" ]; then
    if (cd "$(dirname "$0")/../infra/terraform" && terraform init -backend=true -input=false -no-color >/dev/null 2>&1); then
      ok "terraform-backend (S3 state 到達)"
    else
      skip "terraform-backend" "init 失敗（社内proxy/SSL の既知問題なら apply 時の plan で代替確認）"
    fi
  fi
else
  ng "terraform-cli" "terraform が無い"
fi

# 5) docker buildx（arm64 イメージビルド用）
if command -v docker >/dev/null 2>&1; then
  if docker buildx version >/dev/null 2>&1; then
    ok "docker-buildx"
  else
    ng "docker-buildx" "buildx が無い（Docker Desktop 更新 or buildx インストール）"
  fi
else
  ng "docker" "docker が無い（イメージビルドに必須）"
fi

echo
echo "=== summary: PASS=${#PASS[@]} FAIL=${#FAIL[@]} SKIP=${#SKIP[@]} ==="
if [ "${#FAIL[@]}" -gt 0 ]; then
  printf 'FAIL:\n'; printf '  - %s\n' "${FAIL[@]}"
  echo "→ FAIL を解消してから docs/openclaw/golive_checklist.md を開始してください。"
  exit 1
fi
if [ "${#SKIP[@]}" -gt 0 ]; then
  printf 'SKIP:\n'; printf '  - %s\n' "${SKIP[@]}"
  echo "→ 認証してから再実行を推奨（SKIP のままでも checklist は開始可）。"
  exit 2
fi
echo "→ 全チェック PASS。docs/openclaw/golive_checklist.md を上から開始できます。"
exit 0

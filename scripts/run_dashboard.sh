#!/usr/bin/env bash
# ローカル管理画面を起動する（SSMトンネル自動 + dev-bypass）。
# 使い方:  bash scripts/run_dashboard.sh
# → 起動後、デスクトップのブラウザで http://localhost:8787 を開く（Ctrl+C で停止）。
#
# 前提: AWS 資格情報（Bedrock/RDS/Secrets権限）・nvm不要・.venv 構築済み。
# 認証: 既定は dev-bypass（localhost 限定・ログイン無し）。Web OAuth クライアント作成後は
#       DASHBOARD_DEV_BYPASS=0 + DASHBOARD_GOOGLE_CLIENT_ID/ALLOWED_EMAILS/ALLOWED_HD を設定。
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="ap-northeast-1"
BASTION="i-04fd1f367b454f641"
RDS_HOST="teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com"
LOCAL_PORT="15433"

# 1) SSM ポートフォワード（RDS）が無ければ起動して待つ
if ! nc -z localhost "${LOCAL_PORT}" 2>/dev/null; then
  echo "SSMトンネル(${LOCAL_PORT})を起動..."
  aws ssm start-session --target "${BASTION}" --region "${REGION}" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"${RDS_HOST}\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}" &
  for _ in $(seq 1 15); do nc -z localhost "${LOCAL_PORT}" 2>/dev/null && break; sleep 1; done
fi
nc -z localhost "${LOCAL_PORT}" 2>/dev/null || { echo "ERROR: トンネル確立失敗"; exit 1; }

# 2) DB パスワードを Secrets Manager から取得（シェル履歴に残さない）
PGPW="$(aws secretsmanager get-secret-value --secret-id teamagent/dev/db_password \
  --region "${REGION}" --query SecretString --output text)"

# 3) 管理画面を起動（既定 dev-bypass・localhost:8787）
export DATABASE_URL="postgresql://teamagent:${PGPW}@localhost:${LOCAL_PORT}/teamagent"
export DASHBOARD_DEV_BYPASS="${DASHBOARD_DEV_BYPASS:-1}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8787}"

echo "→ ブラウザで http://localhost:${DASHBOARD_PORT} を開いてください（Ctrl+C で停止）"
PYTHONPATH=src .venv/bin/python -m teamagent.dashboard

#!/usr/bin/env bash
# SSM port-forward 経由で管理用DSNを使い、検索回答の評価サマリを生成する。
# 使い方: bash scripts/answer_rating_summary_tunneled.sh [--days N] [--out PATH] [--with-emails]
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="ap-northeast-1"
BASTION="i-0318356170700582b"
RDS="teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com"
LOCAL_PORT="5435"
SECRET="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/database-url-4pJMDr"

echo "== DB 接続文字列を取得し localhost:${LOCAL_PORT} へ書換（PW非表示）=="
RAW="$(aws secretsmanager get-secret-value --secret-id "$SECRET" --region "$REGION" --query SecretString --output text)"
DB_URL="$(python3 - "$RAW" "$RDS" "$LOCAL_PORT" <<'PY'
import json
import re
import sys

raw, rds, local_port = sys.argv[1], sys.argv[2], sys.argv[3]
url = None
try:
    value = json.loads(raw)
    if isinstance(value, dict):
        url = value.get("DATABASE_URL") or value.get("database_url") or value.get("url")
        if not url and value.get("username"):
            database = value.get("dbname") or value.get("database") or "teamagent"
            url = f"postgresql://{value['username']}:{value.get('password', '')}@{rds}:5432/{database}"
except Exception:
    pass
if url is None:
    url = raw.strip()
print(re.sub(r"@[^/@]+/", f"@localhost:{local_port}/", url))
PY
)"
echo "   OK（接続先 user/dbname は秘匿）"

echo "== SSM port-forward 起動（bastion → RDS:5432 → localhost:${LOCAL_PORT}）=="
aws ssm start-session --target "$BASTION" --region "$REGION" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=$RDS,portNumber=5432,localPortNumber=$LOCAL_PORT" >/tmp/ssm_answer_rating_pf.log 2>&1 &
SSM_PID=$!
trap 'kill "$SSM_PID" 2>/dev/null || true' EXIT
sleep 10
echo "   tunnel up (pid=$SSM_PID)"

export ANSWER_RATING_DB_URL="$DB_URL"

# TODO: 匿名化済み出力のSlack投稿は、将来 openclaw/ops 基盤へ相乗りして実装する。
uv run python scripts/answer_rating_summary.py "$@"

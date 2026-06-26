#!/usr/bin/env bash
# SSM port-forward で本番 RDS に繋ぎ、forward-only migration を流す。
# 使い方:  bash migrate_tunneled.sh dry     # 適用予定だけ表示（まずこれ）
#          bash migrate_tunneled.sh apply   # 実適用（未適用分のみ・idempotent）
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-northeast-1
MODE=${1:-dry}
BASTION=i-0318356170700582b
RDS=teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com
LPORT=5433
SECRET=arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/database-url-4pJMDr

echo "== DB 接続文字列を取得し localhost:${LPORT} へ書換（PW非表示）=="
RAW=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text)
DB_URL=$(python3 - "$RAW" "$RDS" "$LPORT" <<'PY'
import sys, json, re
raw, rds, lport = sys.argv[1], sys.argv[2], sys.argv[3]
url = None
try:
    obj = json.loads(raw)
    if isinstance(obj, dict):
        url = obj.get("DATABASE_URL") or obj.get("database_url") or obj.get("url")
        if not url and obj.get("username"):
            u = obj["username"]; p = obj.get("password", "")
            db = obj.get("dbname") or obj.get("database") or "teamagent"
            url = f"postgresql://{u}:{p}@{rds}:5432/{db}"
except Exception:
    pass
if url is None:
    url = raw.strip()
url = re.sub(r"@[^/@]+/", f"@localhost:{lport}/", url)
print(url)
PY
)
echo "   OK（接続先 user/dbname は秘匿）"

echo "== SSM port-forward 起動（bastion → RDS:5432 → localhost:${LPORT}）=="
aws ssm start-session --target "$BASTION" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=$RDS,portNumber=5432,localPortNumber=$LPORT" >/tmp/ssm_pf_migrate.log 2>&1 &
SSM_PID=$!
trap 'kill $SSM_PID 2>/dev/null || true' EXIT
sleep 10
echo "   tunnel up (pid=${SSM_PID})"

export DATABASE_URL="$DB_URL"
if [ "$MODE" = "apply" ]; then
  echo "== migration 実適用（未適用分のみ）=="
  uv run python scripts/migrate.py
else
  echo "== migration ドライラン（適用予定の表示のみ）=="
  uv run python scripts/migrate.py --dry-run
fi

#!/usr/bin/env bash
# 金庫（pgvector）の documents をタイトルの語で数える・読み取りだけ（2026-10-06）。
#   bash verify_vault_docs_readonly.sh 事例集 実績 結果
# READ ONLY トランザクションで SELECT だけ。秘密値は表示しない。本文は出さない（タイトル・種類・topic だけ）。
set -euo pipefail

REGION=ap-northeast-1
BASTION=i-0318356170700582b
RDS_HOST=teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com
LOCAL_PORT=15448
PY="$HOME/dev/worktrees/teamagent-build/.venv/bin/python"
[ -x "$PY" ] || { echo "python が無い: $PY"; exit 1; }
aws ssm start-session --region $REGION --target $BASTION \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
  >/dev/null 2>&1 &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do nc -z 127.0.0.1 $LOCAL_PORT 2>/dev/null && break; sleep 1; done
nc -z 127.0.0.1 $LOCAL_PORT || { echo "トンネルが開かない"; exit 1; }
PGPW=$(aws secretsmanager get-secret-value --region $REGION --secret-id teamagent/dev/db_password \
  --query SecretString --output text)
TERMS="$*"
export PGPW LOCAL_PORT TERMS
"$PY" - <<'PYEOF'
import os
import psycopg

dsn = f"postgresql://teamagent:{os.environ['PGPW']}@127.0.0.1:{os.environ['LOCAL_PORT']}/teamagent?sslmode=require"
import json
with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("SET LOCAL app.user_role = 'admin'")
    cur.execute("""SELECT coalesce(user_email,user_id), extract(epoch from occurred_at)::bigint, skill
                     FROM usage_events WHERE occurred_at > now() - interval '60 days'
                      AND skill NOT IN ('operation_log','unknown') ORDER BY 1, 2""")
    rows = cur.fetchall()
    conn.rollback()
json.dump(rows, open(os.path.expanduser("~/Documents/Claude/Artifacts/aico-usage-20261006/calls_60d.json"), "w"))
print("rows", len(rows))
PYEOF

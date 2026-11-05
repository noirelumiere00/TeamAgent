#!/usr/bin/env bash
# 本番 RDS に読み取り専用の SQL を流す（SET TRANSACTION READ ONLY・書き込み不可）。
#   ./db_readonly_query.sh <file.sql>
#
#   ./apply_0030_digest_prefs.sh           # 読み取りだけ（適用済みの版・表の有無を表示）
#   ./apply_0030_digest_prefs.sh --apply   # 0030 を 1 トランザクションで適用＋schema_migrations に記録＋検証
#
# なぜ scripts/migrate.py を使わないか: migrate.py は「未適用分を全部」流す。0029（本人メモ）が
# 未適用なら一緒に当たってしまう（0029 は点灯前にロール属性を実測してから当てる約束）。
# ここでは 0030 だけを、migrate.py と同じ記録形式（version/filename/checksum_sha）で当てる
# ＝後で migrate.py を流しても 0030 は skip される。
#
# 経路: SSM ポートフォワード（踏み台 teamagent-dev-bastion）→ RDS teamagent-dev。
# パスワードは Secrets Manager から実行時に読み、画面・ファイルには出さない。
set -euo pipefail

REGION=ap-northeast-1
BASTION=i-0318356170700582b
RDS_HOST=teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com
LOCAL_PORT=15433
WT=${WT:-$HOME/dev/worktrees/teamagent-digest-prefs}
SQL="${1:?SQL ファイルを指定}"
PY="$WT/.venv/bin/python"
MODE=readonly

[ -f "$SQL" ] || { echo "SQL が無い: $SQL"; exit 1; }
[ -x "$PY" ] || { echo "python が無い: $PY"; exit 1; }

echo "== 踏み台: $(aws ec2 describe-instances --region $REGION --instance-ids $BASTION \
  --query 'Reservations[0].Instances[0].State.Name' --output text)"

aws ssm start-session --region $REGION --target $BASTION \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
  >/dev/null 2>&1 &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  nc -z 127.0.0.1 $LOCAL_PORT 2>/dev/null && break
  sleep 1
done
nc -z 127.0.0.1 $LOCAL_PORT || { echo "トンネルが開かない"; exit 1; }

PGPW=$(aws secretsmanager get-secret-value --region $REGION --secret-id teamagent/dev/db_password \
  --query SecretString --output text)
export PGPW MODE SQL

"$PY" - <<'PYEOF'
import os, psycopg
dsn = f"postgresql://teamagent:{os.environ['PGPW']}@127.0.0.1:15433/teamagent?sslmode=require"
stmts = [s.strip() for s in open(os.environ["SQL"], encoding="utf-8").read().split(";") if s.strip()]
with psycopg.connect(dsn, autocommit=False) as conn:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        for s in stmts:
            print("---", s.splitlines()[0][:100])
            cur.execute(s)
            if cur.description:
                for row in cur.fetchall():
                    print(" | ".join(str(c) for c in row))
    conn.rollback()
PYEOF

"""ローカル pgvector のデータを本番 RDS（東京）に移行するスクリプト。

前提:
    - 別シェルで SSM Port Forwarding が起動中（localhost:15432 → 本番 RDS:5432）

      aws ssm start-session \\
        --target i-04fd1f367b454f641 \\
        --document-name AWS-StartPortForwardingSessionToRemoteHost \\
        --parameters host=teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com,\\
                     portNumber=5432,localPortNumber=15432 \\
        --region ap-northeast-1

    - DB_PW 環境変数に Secrets Manager から取得したパスワードを設定
    - docker compose 起動中（ローカルから pg_dump するため）

Usage:
    DB_PW=$(aws secretsmanager get-secret-value \\
        --secret-id teamagent/dev/db_password \\
        --region ap-northeast-1 \\
        --query SecretString --output text)
    python scripts/migrate_to_prod_rds.py

Notes:
    - 本番 RDS に proposals_chunks / proposals_chunks_contextual が既に存在する
      場合は ON CONFLICT スキップで重複しない設計だが、念のため事前確認推奨
    - 約 3 MB（98 chunks × 2 テーブル + INSERT 文）を psycopg で流し込み
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]


PROD_HOST = "localhost"
PROD_PORT = 15432  # SSM port-forward 経由
LOCAL_CONTAINER = "teamagent-postgres"
SCHEMA_DUMP = "/tmp/teamagent_schema.sql"
DATA_DUMP = "/tmp/teamagent_data.sql"
SCHEMA_CLEAN = "/tmp/teamagent_schema_clean.sql"
DATA_CLEAN = "/tmp/teamagent_data_clean.sql"


def dump_local() -> None:
    """ローカル Docker pgvector から pg_dump で schema + data を抽出。"""
    tables = ["-t", "proposals_chunks", "-t", "proposals_chunks_contextual"]
    print("📤 schema をローカルから dump")
    with open(SCHEMA_DUMP, "w") as f:
        subprocess.run(
            [
                "docker", "exec", LOCAL_CONTAINER,
                "pg_dump", "-U", "teamagent", "-d", "teamagent",
                *tables, "--schema-only",
            ],
            stdout=f,
            check=True,
        )
    print("📤 data をローカルから dump")
    with open(DATA_DUMP, "w") as f:
        subprocess.run(
            [
                "docker", "exec", LOCAL_CONTAINER,
                "pg_dump", "-U", "teamagent", "-d", "teamagent",
                *tables, "--data-only", "--column-inserts",
            ],
            stdout=f,
            check=True,
        )

    # psql メタコマンド（\restrict 等）を psycopg が解釈できないので除去
    for src, dst in [(SCHEMA_DUMP, SCHEMA_CLEAN), (DATA_DUMP, DATA_CLEAN)]:
        with open(src) as fi, open(dst, "w") as fo:
            for line in fi:
                if not line.startswith("\\"):
                    fo.write(line)
        print(f"  ✅ {dst}")


def restore_to_prod(dsn: str) -> None:
    """schema + data を本番 RDS に流し込む。"""
    # schema
    print("📥 schema を本番 RDS に流し込み")
    with open(SCHEMA_CLEAN) as f:
        schema_sql = f.read()
    with psycopg.connect(dsn, autocommit=True) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    print("  ✅ schema OK")

    # data
    print(f"📥 data 流し込み中（{os.path.getsize(DATA_CLEAN):,} bytes）")
    with open(DATA_CLEAN) as f:
        data_sql = f.read()
    with psycopg.connect(dsn) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(data_sql)
        conn.commit()
    print("  ✅ data OK")


def verify(dsn: str) -> None:
    """本番 RDS の件数を確認する。"""
    print("🔍 本番 RDS の件数確認")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for t in ["proposals_chunks", "proposals_chunks_contextual"]:
                cur.execute(f"SELECT count(*) FROM {t}")
                print(f"  {t}: {cur.fetchone()[0]}")
            cur.execute(
                "SELECT count(*) FROM proposals_chunks_contextual WHERE metadata IS NOT NULL"
            )
            print(f"  metadata 付き: {cur.fetchone()[0]}")
            cur.execute(
                "SELECT count(*) FROM proposals_chunks_contextual WHERE drive_url IS NOT NULL"
            )
            print(f"  drive_url 付き: {cur.fetchone()[0]}")


def main() -> int:
    db_pw = os.environ.get("DB_PW")
    if not db_pw:
        print("❌ DB_PW 環境変数が未設定（Secrets Manager から取得して export してください）")
        return 1

    dsn = f"postgresql://teamagent:{db_pw}@{PROD_HOST}:{PROD_PORT}/teamagent?sslmode=require"

    print(f"🚀 ローカル pgvector → 本番 RDS（東京）migration 開始\n")

    # Step 0: トンネル接続確認
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                ver = cur.fetchone()[0]
                print(f"  接続 OK: {ver[:60]}")
    except Exception as e:
        print(f"❌ 本番 RDS に接続できません: {e}")
        print("  SSM Port Forwarding が動いているか確認してください")
        return 1

    dump_local()
    restore_to_prod(dsn)
    verify(dsn)
    print("\n🎉 migration 完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())

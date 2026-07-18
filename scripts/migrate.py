"""Forward-only DB migrations runner.

Sprint 3 / PR-1 で導入。`infra/migrations/*.sql` を順番に適用、
`schema_migrations` テーブルで applied_at を管理する idempotent runner。

設計:
- 一方向のみ（rollback なし）。down は手動 ALTER で対応
- ファイル名は `NNNN_description.sql`（NNNN は 4 桁ゼロ詰め通し番号）
- 同一ファイルは 2 回適用されない（既に applied_at がある場合 skip）
- 各 SQL は単一 transaction 内で実行、失敗時は全 rollback

Usage:
    # 適用（未適用分のみ）
    python scripts/migrate.py

    # ドライラン（適用予定だけ表示）
    python scripts/migrate.py --dry-run

    # 強制再適用（既存マーク削除 + 再実行）— 開発のみ
    python scripts/migrate.py --rerun 0001

前提:
    DATABASE_URL が環境変数で設定済み（scripts/load_secrets.sh または .env.local 等）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "infra" / "migrations"
_TRANSACTION_CONTROL_RE = re.compile(
    r"(?im)^\s*(?:"
    r"BEGIN(?:\s+(?:WORK|TRANSACTION))?|"
    r"START\s+TRANSACTION|"
    r"COMMIT(?:\s+WORK)?|"
    r"ROLLBACK(?:\s+WORK)?"
    r")\s*;"
)


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      TEXT PRIMARY KEY,           -- 0001, 0002, ...
    filename     TEXT NOT NULL,
    checksum_sha TEXT NOT NULL,              -- 適用時の SHA-256（改竄検知）
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _list_migrations() -> list[tuple[str, Path]]:
    """`infra/migrations/NNNN_*.sql` を version 昇順で返す。"""
    if not MIGRATIONS_DIR.exists():
        return []
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    out: list[tuple[str, Path]] = []
    for p in files:
        version = p.name.split("_", 1)[0]
        out.append((version, p))
    return out


def _applied_versions(conn: psycopg.Connection[tuple[str, ...]]) -> dict[str, str]:
    """{version: checksum_sha} を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum_sha FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def _apply_one(
    conn: psycopg.Connection[tuple[str, ...]],
    version: str,
    path: Path,
    *,
    rerun: bool = False,
) -> None:
    """1 つの migration をrunner所有transactionで適用する。"""
    sql = path.read_text(encoding="utf-8")
    if conn.autocommit:
        raise RuntimeError("migration connection must use autocommit=False")
    if _TRANSACTION_CONTROL_RE.search(sql):
        raise RuntimeError(
            f"{path.name} contains transaction control; scripts/migrate.py owns the transaction"
        )
    checksum = _sha256(sql)
    with conn.cursor() as cur:
        if rerun:
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        # SQL 本体を実行（複文 OK）
        cur.execute(sql)
        # 適用記録
        cur.execute(
            "INSERT INTO schema_migrations (version, filename, checksum_sha, applied_at) "
            "VALUES (%s, %s, %s, %s)",
            (version, path.name, checksum, datetime.now(UTC)),
        )


def run(
    *,
    dry_run: bool = False,
    rerun_version: str | None = None,
    dsn: str | None = None,
) -> int:
    """全 migration を順次適用。返り値は exit code（0=成功, 1=失敗）。"""
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] DATABASE_URL が未設定です", file=sys.stderr)
        return 2

    migrations = _list_migrations()
    if not migrations:
        print(f"[INFO] {MIGRATIONS_DIR} に migration ファイルがありません")
        return 0

    print(f"[INFO] discovered {len(migrations)} migration(s)")

    with psycopg.connect(dsn, autocommit=False) as conn:
        # bootstrapping: schema_migrations テーブル自体を idempotent に作成
        with conn.cursor() as cur:
            cur.execute(SCHEMA_MIGRATIONS_DDL)

        applied = _applied_versions(conn)
        if dry_run:
            # Dry-run may bootstrap schema_migrations for inspection, but must leave no DB writes.
            conn.rollback()
        else:
            conn.commit()

        for version, path in migrations:
            if version == rerun_version:
                print(f"[FORCE-RERUN] {path.name}")
                if dry_run:
                    continue
                try:
                    _apply_one(conn, version, path, rerun=True)
                    conn.commit()
                    print("  → applied (rerun)")
                except Exception as e:
                    conn.rollback()
                    print(f"[ERROR] {path.name} failed: {e}", file=sys.stderr)
                    return 1
                continue

            if version in applied:
                # 改竄検知（適用済だが内容が変わっている）
                current_sha = _sha256(path.read_text(encoding="utf-8"))
                if current_sha != applied[version]:
                    print(
                        f"[ERROR] {path.name} は適用済だが内容が変わっています "
                        f"(stored={applied[version][:8]}…, current={current_sha[:8]}…). "
                        "適用済 migration は変更せず、新 version で forward fix してください。",
                        file=sys.stderr,
                    )
                    return 1
                print(f"[SKIP] {path.name} (already applied, checksum={current_sha})")
                continue

            print(f"[APPLY] {path.name}")
            if dry_run:
                continue
            try:
                _apply_one(conn, version, path)
                conn.commit()
                print("  → applied")
            except Exception as e:
                conn.rollback()
                print(f"[ERROR] {path.name} failed: {e}", file=sys.stderr)
                return 1

    print("[OK] all migrations processed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="適用予定だけ表示")
    p.add_argument("--rerun", metavar="VERSION", help="指定 version を強制再適用（例: 0001）")
    args = p.parse_args()
    return run(dry_run=args.dry_run, rerun_version=args.rerun)


if __name__ == "__main__":
    sys.exit(main())

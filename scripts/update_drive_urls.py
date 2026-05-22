"""data/proposal_drive_map.json から Drive URL を読み込み、pgvector に反映する。

proposals_chunks と proposals_chunks_contextual の両テーブルに drive_url 列を ALTER で
追加（IF NOT EXISTS）し、file_name でマッピングして UPDATE する。

Usage:
    python scripts/update_drive_urls.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import psycopg  # noqa: E402

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)
DRIVE_MAP_PATH = PROJECT_ROOT / "data" / "proposal_drive_map.json"
TABLES = ["proposals_chunks", "proposals_chunks_contextual"]


def main() -> int:
    if not DRIVE_MAP_PATH.exists():
        print(f"❌ {DRIVE_MAP_PATH} が見つかりません")
        print("  proposal_drive_map.example.json をコピーして実 URL を入れてください")
        return 1

    with DRIVE_MAP_PATH.open(encoding="utf-8") as f:
        mapping: dict[str, Any] = json.load(f)

    # コメントキー（_comment, _format 等）を除外
    mapping = {k: v for k, v in mapping.items() if not k.startswith("_")}

    if not mapping:
        print("❌ マッピングが空です")
        return 1

    print(f"📋 {len(mapping)} 件のマッピング:")
    for k, v in mapping.items():
        print(f"  {k} → {v[:60]}{'...' if len(v) > 60 else ''}")

    conn = psycopg.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS drive_url TEXT;"
                )
                print(f"✅ {table}.drive_url 列を確認")

            updated_total = 0
            for table in TABLES:
                for file_name, url in mapping.items():
                    cur.execute(
                        f"UPDATE {table} SET drive_url = %s WHERE file_name = %s;",
                        (url, file_name),
                    )
                    print(
                        f"  {table}: {file_name} → updated rows={cur.rowcount}"
                    )
                    updated_total += cur.rowcount
        conn.commit()
        print(f"\n🎉 完了：合計 {updated_total} 行を更新")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

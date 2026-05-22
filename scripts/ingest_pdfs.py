"""PDF 取り込みパイプライン（自動化版）。

data/proposals/ をスキャンして、まだ pgvector に未登録の PDF だけを処理する。
処理内容：
  1. PDF テキスト抽出（pdfplumber）+ チャンク化
  2. multilingual-e5-large で embedding → proposals_chunks
  3. Haiku 4.5 で contextualize → proposals_chunks_contextual
  4. Sonnet 4.6 でメタデータ抽出 → metadata JSONB
  5. data/proposal_drive_map.json から drive_url を反映

Usage:
    python scripts/ingest_pdfs.py [--force]

--force を付けると既存データを無視して全 PDF を再処理。

前提:
    - docker compose up（ローカル pgvector）
    - data/proposals/ に PDF が置いてある
    - AWS_REGION / BEDROCK_MODEL_ID / BEDROCK_HAIKU_MODEL_ID 設定
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]  # noqa: E402

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)
DATA_DIR = PROJECT_ROOT / "data" / "proposals"
DRIVE_MAP_PATH = PROJECT_ROOT / "data" / "proposal_drive_map.json"


def scan_local_pdfs() -> list[Path]:
    """data/proposals/ 内の PDF を列挙。"""
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.glob("*.pdf"))


def already_ingested(conn: psycopg.Connection[Any], file_name: str) -> bool:
    """proposals_chunks に既存があるかチェック。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM proposals_chunks WHERE file_name = %s LIMIT 1",
            (file_name,),
        )
        return cur.fetchone() is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存データを無視して全 PDF を再処理",
    )
    args = parser.parse_args()

    pdfs = scan_local_pdfs()
    if not pdfs:
        print(f"❌ {DATA_DIR} に PDF が見つかりません")
        return 1

    print(f"📂 ローカル PDF: {len(pdfs)} 件")
    for p in pdfs:
        print(f"  - {p.name}")

    # 既存判定
    conn = psycopg.connect(DB_DSN)
    register_vector(conn)
    try:
        if args.force:
            new_pdfs = pdfs
            print("⚠️ --force 指定: 全 PDF を再処理")
        else:
            new_pdfs = [p for p in pdfs if not already_ingested(conn, p.name)]
            if not new_pdfs:
                print("✅ すべての PDF が既に取り込み済み。--force で再処理可能")
            else:
                print(f"\n🆕 未処理 PDF: {len(new_pdfs)} 件")
                for p in new_pdfs:
                    print(f"  - {p.name}")
    finally:
        conn.close()

    if not new_pdfs:
        print("\n📊 後続パイプラインも既存データに対して念のため再走させます")

    # 1. chunking + embedding → proposals_chunks
    print(f"\n{'=' * 70}")
    print("Step 1/4: PDF chunking + embedding → proposals_chunks")
    print("=" * 70)
    ret = os.system("python scripts/demo_pdf_vectorize.py")
    if ret != 0:
        print(f"❌ Step 1 失敗 (exit {ret})")
        return 1

    # 2. contextual retrieval（既存 chunks に対して未処理だけ）
    print(f"\n{'=' * 70}")
    print("Step 2/4: Contextual Retrieval (Haiku 4.5)")
    print("=" * 70)
    ret = os.system("python scripts/contextual_retrieval.py")
    if ret != 0:
        print(f"❌ Step 2 失敗 (exit {ret})")
        return 1

    # 3. メタデータ抽出
    print(f"\n{'=' * 70}")
    print("Step 3/4: メタデータ抽出 (Sonnet 4.6)")
    print("=" * 70)
    ret = os.system("python scripts/extract_metadata.py")
    if ret != 0:
        print(f"❌ Step 3 失敗 (exit {ret})")
        return 1

    # 4. Drive URL 反映
    print(f"\n{'=' * 70}")
    print("Step 4/4: Drive URL 反映")
    print("=" * 70)
    if not DRIVE_MAP_PATH.exists():
        print(f"⚠️ {DRIVE_MAP_PATH} が無いので skip")
    else:
        ret = os.system("python scripts/update_drive_urls.py")
        if ret != 0:
            print(f"⚠️ Step 4 失敗 (exit {ret}) - 続行")

    # 最終確認
    conn = psycopg.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM proposals_chunks")
            n_normal = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM proposals_chunks_contextual")
            n_ctx = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM proposals_chunks_contextual WHERE metadata IS NOT NULL"
            )
            n_meta = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM proposals_chunks_contextual WHERE drive_url IS NOT NULL"
            )
            n_drive = cur.fetchone()[0]
        print("\n🎉 取り込みパイプライン完了")
        print(f"  proposals_chunks            : {n_normal}")
        print(f"  proposals_chunks_contextual : {n_ctx}")
        print(f"  ↑ metadata 付き             : {n_meta}")
        print(f"  ↑ drive_url 付き            : {n_drive}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

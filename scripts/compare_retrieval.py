"""通常検索 vs Contextual Retrieval の精度比較スクリプト。

同じクエリを両テーブルで実行し、top-5 ヒットの similarity score を並べる。

A/B（e5 vs Bedrock Cohere）比較も可能:
  - EMBEDDER_BACKEND=cohere EMBEDDING_COLUMN=embedding_cohere（env でペア指定）
  - --embedding-col embedding_cohere（旧スキーマ proposals_chunks の embedding 列を切替）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]  # noqa: E402

from teamagent.adapters.embeddings_client import (  # noqa: E402
    ALLOWED_EMBEDDING_COLUMNS,
    Embedder,
    build_embedder_from_env,
)

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)

QUERIES = [
    "PR代行の業界別実績は？",
    "INPEX案件の提案内容を教えて",
    "ショート動画のアルゴリズムについて",
    "Z世代のメディア利用変化",
    "ベクトル社のサービス概要",
]


def search(
    conn: psycopg.Connection,
    query: str,
    embedder: Embedder,
    table: str,
    content_col: str,
    limit: int = 5,
    embedding_col: str = "embedding",
) -> list[tuple[int, float, str, str]]:
    """ベクトル検索を実行し、(chunk_id, score, file_name, content_head) を返す。

    embedding_col は SQL 識別子として埋めるため固定許可リストのみ受け付ける（injection 防止）。
    """
    if embedding_col not in ALLOWED_EMBEDDING_COLUMNS:
        raise ValueError(f"embedding_col は {sorted(ALLOWED_EMBEDDING_COLUMNS)} のいずれか")
    qvec = embedder.embed(query)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, 1 - ({embedding_col} <=> %s::vector) AS score, file_name, {content_col}
            FROM {table}
            ORDER BY {embedding_col} <=> %s::vector
            LIMIT %s
            """,  # nosec B608
            (qvec, qvec, limit),
        )
        rows = cur.fetchall()
    return [(r[0], float(r[1]), r[2], r[3][:60].replace("\n", " ")) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--embedding-col",
        default="embedding",
        choices=sorted(ALLOWED_EMBEDDING_COLUMNS),
        help="検索する embedding 列（既定 embedding＝e5）。Cohere A/B は embedding_cohere。",
    )
    args = p.parse_args()

    print("📊 通常 vs Contextual Retrieval 精度比較")
    print(f"  DB: {DB_DSN}")
    # EMBEDDER_BACKEND（既定 local）で local-e5 / Bedrock Cohere を切替（EMBEDDING_COLUMN と整合）。
    embedder = build_embedder_from_env()
    conn = psycopg.connect(DB_DSN)
    register_vector(conn)

    try:
        for i, query in enumerate(QUERIES, 1):
            print(f"\n{'=' * 80}")
            print(f"Q{i}: {query}")
            print("=" * 80)

            normal = search(
                conn,
                query,
                embedder,
                "proposals_chunks",
                "text",
                embedding_col=args.embedding_col,
            )
            ctx = search(
                conn,
                query,
                embedder,
                "proposals_chunks_contextual",
                "contextualized_text",
                embedding_col=args.embedding_col,
            )

            print("\n--- 通常検索（proposals_chunks.text）---")
            for j, (_cid, score, fname, head) in enumerate(normal, 1):
                print(f"  #{j} score={score:.4f} | {fname[:25]:<25} | {head}")

            print("\n--- Contextual（proposals_chunks_contextual.contextualized_text）---")
            for j, (_cid, score, fname, head) in enumerate(ctx, 1):
                print(f"  #{j} score={score:.4f} | {fname[:25]:<25} | {head}")

            top_normal = normal[0][1] if normal else 0
            top_ctx = ctx[0][1] if ctx else 0
            diff = (top_ctx - top_normal) * 100
            print(f"\n  📈 top-1 score 差分: {diff:+.2f} ポイント")
    finally:
        conn.close()

    print("\n🎉 比較完了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
